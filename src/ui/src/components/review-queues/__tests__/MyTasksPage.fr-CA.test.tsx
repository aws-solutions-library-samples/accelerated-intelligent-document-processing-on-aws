// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Home › Tasks and its work table under `fr-CA`. With a fixture catalog the heading, the
 * column headers, the source badges, a production item's low-confidence reason
 * (`0,62 < 0,80`) and a Test Studio document's alert count read French, and so do the work
 * table's state labels, `Vous` for the reader's own items and an empty table's "No data!".
 * The rows hold codes and the table words them, so the source filter keeps its choice across
 * a language switch and relabels it. Under `en` the same renders read today's English.
 * Queue and test set ids stay as stored.
 */
import React from 'react';
import { act, screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { I18n } from 'aws-amplify/utils';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ReviewWorkContextValue } from '../../../contexts/review-work';
import type { ReviewItem, TestSetTask } from '../../../graphql/generated/operation-types';
import { renderWithIntl } from '../../../test-utils/renderWithIntl';

// One value per mocked hook for every render, so no render hands out a fresh object.
const state = vi.hoisted(() => ({
  refresh: vi.fn(),
  reviewWork: { current: null as ReviewWorkContextValue | null },
}));

vi.mock('virtual:i18n-catalogs', () => import('../../../test-utils/i18nCatalogsMock'));
vi.mock('../../../hooks/use-review-work', () => ({ useOptionalReviewWork: () => state.reviewWork.current }));

import MyTasksPage from '../MyTasksPage';
import WorkItemsTable from '../WorkItemsTable';
import { fromReviewItem, fromTestSetTask } from '../work-items';

const MINUTE = 60_000;
const ago = (minutes: number) => new Date(Date.now() - minutes * MINUTE).toISOString();

/** A production item the reader holds (CLAIMED), with a low-confidence reason. */
const heldItem = (): ReviewItem =>
  ({
    objectKey: 'inbound/claims/w2.pdf',
    cycleId: 'c1',
    queue: 'claims',
    escalated: false,
    priority: 5,
    enqueuedAt: ago(90),
    reasons: [{ kind: 'low_confidence', sectionId: '1', field: 'employer_name', label: 'Employer name', score: 0.62, threshold: 0.8 }],
    reasonCount: 1,
    state: 'CLAIMED',
    isMine: true,
    ownerEmail: null,
  }) as unknown as ReviewItem;

/** A production item waiting in its queue, as a queue's page lists it. */
const waitingItem = (): ReviewItem =>
  ({ ...heldItem(), objectKey: 'inbound/claims/w9.pdf', reasons: [], reasonCount: 0, state: 'OPEN', isMine: false }) as ReviewItem;

/** A Test Studio document assigned to the reader, with two confidence alerts. */
const TASK: TestSetTask = { testSetId: 'invoices', objectKey: 'sets/invoices/inv-7.pdf', assignedAt: ago(30), alertCount: 2 };

beforeEach(() => {
  state.refresh.mockReset();
  state.reviewWork.current = {
    myItems: [heldItem()],
    testSetTasks: [fromTestSetTask(TASK)],
    count: 2,
    loaded: true,
    pollError: null,
    refresh: state.refresh,
    refreshTestSetTasks: vi.fn(),
    beginMutation: vi.fn(),
    settle: vi.fn(),
  };
});

afterEach(() => {
  window.localStorage.clear();
  document.documentElement.lang = '';
  I18n.setLanguage('');
});

/** The strings the assertions read; the rest fall back to English, as the app's error policy allows. */
const FR = {
  'reviewQueues.myTasks.title': 'Tâches',
  'reviewQueues.workItemsTable.column.queue': 'File',
  'reviewQueues.workItemsTable.column.priority': 'Priorité',
  'reviewQueues.workItemsTable.column.reason': 'Motif principal',
  'reviewQueues.workItemsTable.column.state': 'État',
  'reviewQueues.workItemsTable.column.owner': 'Annotateur',
  'reviewQueues.workItemsTable.source.all': 'Toutes les sources',
  'reviewQueues.workItem.source.production': 'Production',
  'reviewQueues.workItem.source.testSet': 'Studio de test',
  'reviewQueues.workItem.state.assigned': 'Assigné',
  'reviewQueues.workItem.you': 'Vous',
  'reviewQueues.workItem.alerts': '{count, plural, one {# alerte de confiance} other {# alertes de confiance}}',
  'reviewQueues.reasons.lowConfidenceField': 'Confiance faible : {field} {score, number, ::.00} < {threshold, number, ::.00}',
  'common.reviewStatus.state.open': 'En attente',
  'common.table.noData': 'Aucune donnée !',
};
const FR_CA = { locale: 'fr-CA', enabledLocales: ['en', 'fr-CA'], messages: FR } as const;

const Router = ({ children }: { children: React.ReactNode }) => <MemoryRouter>{children}</MemoryRouter>;

const table = () => {
  const found = createWrapper(document.body).findTable();
  if (!found) throw new Error('table not rendered');
  return found;
};
const cellText = (row: number, column: number) => table().findBodyCell(row, column)?.getElement().textContent;
const columnHeaders = () =>
  table()
    .findColumnHeaders()
    .map((h) => h.getElement().textContent);
const sourceFilter = () => {
  const found = createWrapper(document.body).findSelect();
  if (!found) throw new Error('source filter not rendered');
  return found;
};
const chosenSource = () => sourceFilter().findTrigger().getElement().textContent;

// Home › Tasks columns: Document, Source, Queue, Priority, Age, Top reason, then the row menu.
const TASK_SOURCE = 2;
const TASK_REASON = 6;

/** The reader's two items and a waiting one, in the table with every column. */
const workRows = () => [fromReviewItem('tasks')(heldItem()), fromTestSetTask(TASK), fromReviewItem('review')(waitingItem())];
// Every column: Document, Source, Queue, Priority, Age, Top reason, State, Annotator.
const STATE = 7;
const OWNER = 8;

describe('Home › Tasks under fr-CA', () => {
  it('reads its heading, the column headers, the source badges and both reasons in French', async () => {
    renderWithIntl(<MyTasksPage />, { ...FR_CA, wrapper: Router });
    // Awaited: the catalog arrives asynchronously, as the chunk does in the browser.
    expect(await screen.findByText('Tâches')).toBeInTheDocument();
    expect(columnHeaders()).toEqual(expect.arrayContaining(['File', 'Priorité', 'Motif principal']));
    expect(columnHeaders()).not.toContain('Top reason');
    expect([cellText(1, TASK_SOURCE), cellText(2, TASK_SOURCE)]).toEqual(['Production', 'Studio de test']);
    expect(cellText(1, TASK_REASON)).toBe('Confiance faible : Employer name 0,62 < 0,80');
    expect(cellText(2, TASK_REASON)).toBe('2 alertes de confiance');
    // The rows come from the shared poll; the catalog's arrival asks it for nothing.
    expect(state.refresh).not.toHaveBeenCalled();
  });

  it('keeps the chosen source across a language switch and relabels it', async () => {
    const view = renderWithIntl(<MyTasksPage />, { ...FR_CA, wrapper: Router });
    expect(await screen.findByText('Tâches')).toBeInTheDocument();
    expect(chosenSource()).toBe('Toutes les sources');
    act(() => sourceFilter().openDropdown());
    act(() => sourceFilter().selectOptionByValue('test-set'));
    expect(chosenSource()).toBe('Studio de test');
    expect(table().findRows()).toHaveLength(1);
    view.setLocale('en');
    await waitFor(() => expect(chosenSource()).toBe('Test Studio'));
    expect(table().findRows()).toHaveLength(1);
    expect(cellText(1, TASK_REASON)).toBe('2 confidence alerts');
  });
});

describe('the work table under fr-CA', () => {
  it('reads its state labels and Vous for the reader’s items in French', async () => {
    renderWithIntl(<WorkItemsTable items={workRows()} title="Items" />, { ...FR_CA, wrapper: Router });
    expect(await screen.findByText('État')).toBeInTheDocument();
    expect(columnHeaders()).toContain('Annotateur');
    expect([1, 2, 3].map((row) => cellText(row, STATE))).toEqual(['Assigné', 'Assigné', 'En attente']);
    expect([1, 2, 3].map((row) => cellText(row, OWNER))).toEqual(['Vous', 'Vous', '—']);
  });

  it('says "No data!" in French when it has no rows and no empty text of its own', async () => {
    renderWithIntl(<WorkItemsTable items={[]} title="Items" />, { ...FR_CA, wrapper: Router });
    expect(await screen.findByText('Aucune donnée !')).toBeInTheDocument();
    expect(screen.queryByText('No data!')).toBeNull();
  });
});

describe('Home › Tasks under en', () => {
  it('reads the inline English for its heading, the column headers, the source badges and both reasons', async () => {
    renderWithIntl(<MyTasksPage />, { wrapper: Router });
    expect(await screen.findByText('Tasks')).toBeInTheDocument();
    expect(columnHeaders()).toEqual(expect.arrayContaining(['Queue', 'Priority', 'Top reason']));
    expect([cellText(1, TASK_SOURCE), cellText(2, TASK_SOURCE)]).toEqual(['Production', 'Test Studio']);
    expect(cellText(1, TASK_REASON)).toBe('Low confidence: Employer name 0.62 < 0.80');
    expect(cellText(2, TASK_REASON)).toBe('2 confidence alerts');
    expect(chosenSource()).toBe('All sources');
  });
});

describe('the work table under en', () => {
  it('reads its state labels and You for the reader’s items in English', () => {
    renderWithIntl(<WorkItemsTable items={workRows()} title="Items" />, { wrapper: Router });
    expect(columnHeaders()).toEqual(expect.arrayContaining(['State', 'Annotator']));
    expect([1, 2, 3].map((row) => cellText(row, STATE))).toEqual(['Assigned', 'Assigned', 'Waiting']);
    expect([1, 2, 3].map((row) => cellText(row, OWNER))).toEqual(['You', 'You', '—']);
  });

  it('says "No data!" when it has no rows and no empty text of its own', () => {
    renderWithIntl(<WorkItemsTable items={[]} title="Items" />, { wrapper: Router });
    expect(screen.getByText('No data!')).toBeInTheDocument();
  });
});
