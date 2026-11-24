// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * A production review queue's page under `fr-CA`. For an Admin, with a fixture catalog, Key
 * Metrics, the Queue Progress bar's sentence (for one document and for five), the Annotators
 * table, the toolbar with More actions, a row's Admin menu with a disabled reason and the
 * Unassign dialog read French. For an Annotator, Claim, Next item, the notice when Next item
 * claimed nothing because others were first, and the notices after a claim of several items
 * of which one succeeded read French; the reason a single claim failed stays English until
 * the API error sentences are translated. The queue's items are requested once, not again
 * when the catalog arrives. Under `en` the same renders read today's English, with the two
 * counts of one in the singular. Queue names and ids stay as stored.
 */
import React from 'react';
import { act, fireEvent, screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import type { ButtonDropdownWrapper, ModalWrapper } from '@cloudscape-design/components/test-utils/dom';
import { I18n } from 'aws-amplify/utils';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ReviewItem } from '../../../graphql/generated/operation-types';
import { renderWithIntl } from '../../../test-utils/renderWithIntl';

// One value per mocked hook for every render, so no render hands out a fresh object.
const state = vi.hoisted(() => {
  const admin = { isAdmin: true, canReview: true, loading: false };
  const annotator = { isAdmin: false, canReview: false, loading: false };
  return {
    admin,
    annotator,
    role: admin,
    app: { setSuccessMessage: vi.fn(), setErrorMessage: vi.fn() },
    listReviewQueues: vi.fn(),
    fetchQueueItems: vi.fn(),
    fetchQueuesOverview: vi.fn(),
    fetchMyReviewQueues: vi.fn(),
    claimItem: vi.fn(),
    claimNextItem: vi.fn(),
  };
});

vi.mock('virtual:i18n-catalogs', () => import('../../../test-utils/i18nCatalogsMock'));
vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: vi.fn() }) }));
vi.mock('../../../contexts/app', () => ({ default: () => state.app }));
vi.mock('../../../hooks/use-user-role', () => ({ default: () => state.role }));
vi.mock('../../../hooks/use-review-work', () => ({ useOptionalReviewWork: () => null }));
vi.mock('../reviewer-assignment', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../reviewer-assignment')>()),
  listReviewQueues: () => state.listReviewQueues(),
  listAnnotatorEmails: async () => [],
}));
vi.mock('../review-api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../review-api')>()),
  fetchQueueItems: (variables: unknown) => state.fetchQueueItems(variables),
  fetchQueuesOverview: () => state.fetchQueuesOverview(),
  fetchMyReviewQueues: (itemLimit: unknown) => state.fetchMyReviewQueues(itemLimit),
  claimItem: (item: unknown) => state.claimItem(item),
  claimNextItem: (queueName: unknown) => state.claimNextItem(queueName),
}));
// The bulk dialog has its own tests and is not opened here.
vi.mock('../../review-bulk/BulkSkipModal', () => ({ default: () => null }));

import ReviewQueuePage from '../ReviewQueuePage';

const MINUTE = 60_000;
const ago = (minutes: number) => new Date(Date.now() - minutes * MINUTE).toISOString();

/** An item of the Default queue, waiting unless `over` says otherwise; no Annotator is named. */
const reviewItem = (name: string, over: Record<string, unknown> = {}): ReviewItem =>
  ({
    objectKey: `in/${name}`,
    cycleId: `c-${name}`,
    profile: 'default',
    queue: 'default',
    priority: 50,
    state: 'OPEN',
    enqueuedAt: ago(30),
    reasons: [],
    reasonCount: 0,
    isMine: false,
    ownerEmail: null,
    ...over,
  }) as unknown as ReviewItem;

const DEFAULT_QUEUE = {
  id: 'default',
  name: 'Default',
  description: '',
  priority: 50,
  checklist: [],
  rejectReasons: [],
  skipReasons: [],
  timeLimitMinutes: 60,
  eligibleAnnotators: [],
  isDefaultQueue: true,
  enabled: true,
  version: 1,
};

/** The Admin's overview of the Default queue: `unclaimed` waiting and `claimed` assigned, one of them started. */
const overview = (unclaimed: number, claimed: number) => ({
  queues: [
    {
      summary: { name: 'default', displayName: 'Default', depth: unclaimed + claimed, unclaimed, claimed, inProgress: claimed > 0 ? 1 : 0 },
      reviewers: [],
    },
  ],
});

/** The Annotator's own summary of the Default queue, which lists them. */
const MY_QUEUES = {
  queues: [
    { name: 'default', displayName: 'Default', description: null, unclaimed: 2, claimed: 0, claimedByMe: 0, oldestEnqueuedAt: null },
  ],
};

beforeEach(() => {
  state.role = state.admin;
  state.listReviewQueues.mockReset().mockResolvedValue([DEFAULT_QUEUE]);
  state.fetchQueueItems
    .mockReset()
    .mockResolvedValue({ items: [reviewItem('a.pdf'), reviewItem('b.pdf', { state: 'CLAIMED' })], nextToken: null });
  state.fetchQueuesOverview.mockReset().mockResolvedValue(overview(2, 3));
  state.fetchMyReviewQueues.mockReset().mockResolvedValue(MY_QUEUES);
  state.claimItem.mockReset();
  state.claimNextItem.mockReset();
  state.app.setSuccessMessage.mockReset();
  state.app.setErrorMessage.mockReset();
});

afterEach(() => {
  window.localStorage.clear();
  document.documentElement.lang = '';
  I18n.setLanguage('');
});

/** The strings the assertions read; the rest fall back to English, as the app's error policy allows. */
const FR = {
  'reviewQueues.queuePage.keyMetrics': 'Indicateurs clés',
  'reviewQueues.queuePage.metric.pending': 'En attente',
  'reviewQueues.queuePage.metric.assigned': 'Assignés',
  'reviewQueues.queuePage.metric.inProgress': 'En cours',
  'reviewQueues.queuePage.metric.annotators': 'Annotateurs',
  'reviewQueues.queuePage.metric.priority': 'Priorité',
  'reviewQueues.queuePage.moreActions': 'Plus d’actions',
  'reviewQueues.queuePage.assignTo': 'Assigner à…',
  'reviewQueues.queuePage.unassign': 'Désassigner',
  'reviewQueues.queuePage.claim': 'Réclamer',
  'reviewQueues.queuePage.nextItem': 'Élément suivant',
  'reviewQueues.queuePage.claimNext.contended': 'D’autres annotateurs ont réclamé les éléments en attente avant vous. Réessayez.',
  'reviewQueues.queuePage.claimedCount':
    'Réclamé {count, plural, one {# élément. Traitez-le} other {# éléments. Traitez-les}} dans Accueil › Tâches.',
  'reviewQueues.queuePage.claimFailed': 'Impossible de réclamer {count, plural, one {# élément} other {# éléments}} : {failures}',
  'reviewQueues.queueProgress.title': 'Progression de la file',
  'reviewQueues.queueProgress.underway':
    '{count} sur {total, plural, one {# document assigné ou en cours} other {# documents assignés ou en cours}}',
  'reviewQueues.annotatorsTable.title': 'Annotateurs de la file',
  'reviewQueues.annotatorsTable.empty': 'Aucun annotateur n’est inscrit sur cette file. Ajoutez-en dans Paramètres.',
  'reviewQueues.workItemsTable.rowActions': 'Actions pour {name}',
  'reviewQueues.supervisorActions.group': 'Administration',
  'reviewQueues.supervisorActions.assign': 'Assigner…',
  'reviewQueues.supervisorActions.unassign': 'Désassigner',
  'reviewQueues.supervisorActions.notAssigned': 'Cet élément n’est pas assigné',
  'reviewQueues.supervisorActions.move': 'Déplacer vers une autre file…',
  'reviewQueues.supervisorActions.remove': 'Retirer de la file…',
  'reviewQueues.supervisorModals.unassign': 'Désassigner',
  'reviewQueues.supervisorModals.cancel': 'Annuler',
  'reviewQueues.supervisorModals.unassignOneNoAnnotator':
    'Reprendre {document} à son annotateur. Il reste dans {queue}, sans assignation; le brouillon est conservé et l’annotateur est prévenu.',
};
const FR_CA = { locale: 'fr-CA', enabledLocales: ['en', 'fr-CA'], messages: FR } as const;

const tables = () => createWrapper(document.body).findAllTables();
/** The Documents table: the page's last table (an Admin also sees the Annotators table above it). */
const itemsTable = () => tables()[tables().length - 1];
const toolbarButton = (text: string) => {
  const found = itemsTable()
    .findHeaderSlot()!
    .findAllButtons()
    .find((b) => b.getElement().textContent === text);
  if (!found) throw new Error(`no toolbar button reading ${text}`);
  return found;
};
const selectRow = (name: string) => {
  const at = itemsTable()
    .findRows()
    .findIndex((r) => r.getElement().textContent?.includes(name));
  act(() =>
    itemsTable()
      .findRowSelectionArea(at + 1)!
      .click(),
  );
};
const metricLabels = () =>
  createWrapper(document.body)
    .findKeyValuePairs()!
    .findItems()
    .map((i) => i.findLabel()?.getElement().textContent);
const containerTitles = () =>
  createWrapper(document.body)
    .findAllContainers()
    .map((c) => c.findHeader()?.getElement().textContent);
const progressText = () => createWrapper(document.body).findProgressBar()!.findAdditionalInfo()!.getElement().textContent;
const openMenu = (label: string): ButtonDropdownWrapper => {
  const menu = createWrapper(document.body)
    .findAllButtonDropdowns()
    .find((d) => d.findNativeButton().getElement().getAttribute('aria-label') === label);
  if (!menu) throw new Error(`no menu labelled ${label}`);
  act(() => menu.openDropdown());
  return menu;
};
/** A menu item's label: its menuitem element, without the hidden disabled reason beside it. */
const itemText = (menu: ButtonDropdownWrapper, id: string) =>
  menu.findItemById(id)?.getElement().querySelector('[role="menuitem"]')?.textContent;
/** A disabled menu item's reason, as a screen reader reads it: the element its aria-describedby names. */
const disabledReason = (menu: ButtonDropdownWrapper, id: string): string | null => {
  const item = menu.findItemById(id)!.getElement();
  const el = item.querySelector('[role="menuitem"]') ?? item;
  expect(el.getAttribute('aria-disabled')).toBe('true');
  const describedBy = el.getAttribute('aria-describedby');
  return describedBy ? (document.getElementById(describedBy)?.textContent ?? null) : null;
};
const findModal = (header: string): Promise<ModalWrapper> =>
  waitFor(() => {
    const found = createWrapper(document.body)
      .findAllModals()
      .find((m) => m.findHeader().getElement().textContent === header && m.isVisible());
    if (!found) throw new Error(`no open dialog titled ${header}`);
    return found;
  });

/** The queue's page once Key Metrics reads `heading` (the catalog has arrived) and both items are listed. */
const renderPage = async (heading: string, options: Parameters<typeof renderWithIntl>[1] = {}) => {
  renderWithIntl(
    <MemoryRouter initialEntries={['/review-queues/queues/default']}>
      <Routes>
        <Route path="/review-queues/queues/:queue" element={<ReviewQueuePage />} />
      </Routes>
    </MemoryRouter>,
    options,
  );
  // Awaited: the catalog arrives asynchronously, as the chunk does in the browser.
  expect(await screen.findByText(heading)).toBeInTheDocument();
  await waitFor(() => expect(itemsTable().findRows()).toHaveLength(2));
};

/** An Annotator's view: both items wait; claiming b.pdf fails. */
const asAnnotator = () => {
  state.role = state.annotator;
  state.fetchQueueItems.mockResolvedValue({ items: [reviewItem('a.pdf'), reviewItem('b.pdf')], nextToken: null });
  state.claimItem.mockImplementation(async (item: ReviewItem) => {
    if (item.objectKey === 'in/b.pdf') throw new Error('INVALID_STATE: the item is not open');
    return item;
  });
};

describe("a production queue's page under fr-CA, for an Admin", () => {
  // The catalog arrives after the first render, as a new `intl`; that must not load the queue again.
  it('reads Key Metrics, Queue Progress, the Annotators table and the toolbar in French, and loads the queue once', async () => {
    await renderPage('Indicateurs clés', FR_CA);
    expect(metricLabels()).toEqual(['En attente', 'Assignés', 'En cours', 'Annotateurs', 'Priorité']);
    expect(containerTitles()).toContain('Progression de la file');
    expect(progressText()).toBe('3 sur 5 documents assignés ou en cours');
    expect(tables()[0].findHeaderSlot()!.getElement().textContent).toContain('Annotateurs de la file');
    expect(tables()[0].findEmptySlot()!.getElement().textContent).toBe(
      'Aucun annotateur n’est inscrit sur cette file. Ajoutez-en dans Paramètres.',
    );
    expect(toolbarButton('Assigner à…')).toBeTruthy();
    expect(toolbarButton('Désassigner')).toBeTruthy();
    expect(itemsTable().findHeaderSlot()!.findButtonDropdown()!.getElement().textContent).toContain('Plus d’actions');
    expect(state.fetchQueueItems).toHaveBeenCalledTimes(1);
    expect(state.listReviewQueues).toHaveBeenCalledTimes(1);
    expect(state.fetchQueuesOverview).toHaveBeenCalledTimes(1);
  });

  it('counts one document in the singular in French', async () => {
    state.fetchQueuesOverview.mockResolvedValue(overview(1, 0));
    await renderPage('Indicateurs clés', FR_CA);
    expect(progressText()).toBe('0 sur 1 document assigné ou en cours');
  });

  it("reads a row's Admin menu in French, with why Unassign is disabled", async () => {
    await renderPage('Indicateurs clés', FR_CA);
    const menu = openMenu('Actions pour a.pdf');
    expect(menu.findOpenDropdown()!.getElement().textContent).toContain('Administration');
    expect(['assign', 'unassign', 'move', 'remove'].map((id) => itemText(menu, id))).toEqual([
      'Assigner…',
      'Désassigner',
      'Déplacer vers une autre file…',
      'Retirer de la file…',
    ]);
    expect(disabledReason(menu, 'unassign')).toBe('Cet élément n’est pas assigné');
  });

  it('asks in French before unassigning an item', async () => {
    await renderPage('Indicateurs clés', FR_CA);
    selectRow('b.pdf');
    fireEvent.click(toolbarButton('Désassigner (1)').getElement());
    const dialog = await findModal('Désassigner');
    expect(dialog.findContent().getElement().textContent).toBe(
      'Reprendre b.pdf à son annotateur. Il reste dans default, sans assignation; le brouillon est conservé et l’annotateur est prévenu.',
    );
    expect(
      dialog
        .findFooter()!
        .findAllButtons()
        .map((b) => b.getElement().textContent),
    ).toEqual(['Annuler', 'Désassigner']);
  });
});

describe("a production queue's page under fr-CA, for an Annotator", () => {
  beforeEach(asAnnotator);

  it('reads Claim and Next item in French, and loads the queue once', async () => {
    await renderPage('Indicateurs clés', FR_CA);
    expect(toolbarButton('Réclamer')).toBeTruthy();
    expect(toolbarButton('Élément suivant')).toBeTruthy();
    expect(state.fetchQueueItems).toHaveBeenCalledTimes(1);
    expect(state.fetchMyReviewQueues).toHaveBeenCalledTimes(1);
  });

  it('says in French that other Annotators were first when Next item claimed nothing', async () => {
    state.claimNextItem.mockResolvedValue({ item: null, reason: 'CONTENDED' });
    await renderPage('Indicateurs clés', FR_CA);
    fireEvent.click(toolbarButton('Élément suivant').getElement());
    await waitFor(() =>
      expect(state.app.setErrorMessage).toHaveBeenCalledWith(
        'D’autres annotateurs ont réclamé les éléments en attente avant vous. Réessayez.',
      ),
    );
  });

  it('reports in French a claim of two items of which one succeeded', async () => {
    await renderPage('Indicateurs clés', FR_CA);
    selectRow('a.pdf');
    selectRow('b.pdf');
    fireEvent.click(toolbarButton('Réclamer (2)').getElement());
    await waitFor(() => expect(state.app.setSuccessMessage).toHaveBeenCalledWith('Réclamé 1 élément. Traitez-le dans Accueil › Tâches.'));
    // The summary is French; why b.pdf failed is an API error sentence, English until it is translated.
    expect(state.app.setErrorMessage).toHaveBeenCalledWith(
      'Impossible de réclamer 1 élément : b.pdf (Failed to claim it: INVALID_STATE: the item is not open)',
    );
  });
});

describe("a production queue's page under en, for an Admin", () => {
  it('reads Key Metrics, Queue Progress, the Annotators table and the toolbar in English, and loads the queue once', async () => {
    await renderPage('Key Metrics');
    expect(metricLabels()).toEqual(['Pending', 'Assigned', 'In Progress', 'Annotators', 'Priority']);
    expect(containerTitles()).toContain('Queue Progress');
    expect(progressText()).toBe('3 of 5 documents assigned or in progress');
    expect(tables()[0].findHeaderSlot()!.getElement().textContent).toContain('Annotators');
    expect(tables()[0].findEmptySlot()!.getElement().textContent).toBe('No Annotators are listed on this queue. Add them in Settings.');
    expect(toolbarButton('Assign to…')).toBeTruthy();
    expect(toolbarButton('Unassign')).toBeTruthy();
    expect(itemsTable().findHeaderSlot()!.findButtonDropdown()!.getElement().textContent).toContain('More actions');
    expect(state.fetchQueueItems).toHaveBeenCalledTimes(1);
  });

  it('counts one document in the singular', async () => {
    state.fetchQueuesOverview.mockResolvedValue(overview(1, 0));
    await renderPage('Key Metrics');
    expect(progressText()).toBe('0 of 1 document assigned or in progress');
  });

  it("reads a row's Admin menu, with why Unassign is disabled", async () => {
    await renderPage('Key Metrics');
    const menu = openMenu('Actions for a.pdf');
    expect(menu.findOpenDropdown()!.getElement().textContent).toContain('Admin');
    expect(['assign', 'unassign', 'move', 'remove'].map((id) => itemText(menu, id))).toEqual([
      'Assign…',
      'Unassign',
      'Move to another queue…',
      'Remove from queue…',
    ]);
    expect(disabledReason(menu, 'unassign')).toBe('This item is not assigned');
  });

  it('asks before unassigning an item', async () => {
    await renderPage('Key Metrics');
    selectRow('b.pdf');
    fireEvent.click(toolbarButton('Unassign (1)').getElement());
    const dialog = await findModal('Unassign');
    expect(dialog.findContent().getElement().textContent).toBe(
      'Take b.pdf back from its Annotator. It stays in default, unassigned; the draft is kept and the Annotator is told.',
    );
    expect(
      dialog
        .findFooter()!
        .findAllButtons()
        .map((b) => b.getElement().textContent),
    ).toEqual(['Cancel', 'Unassign']);
  });
});

describe("a production queue's page under en, for an Annotator", () => {
  beforeEach(asAnnotator);

  it('reads Claim and Next item', async () => {
    await renderPage('Key Metrics');
    expect(toolbarButton('Claim')).toBeTruthy();
    expect(toolbarButton('Next item')).toBeTruthy();
    expect(state.fetchQueueItems).toHaveBeenCalledTimes(1);
  });

  it('says that other Annotators were first when Next item claimed nothing', async () => {
    state.claimNextItem.mockResolvedValue({ item: null, reason: 'CONTENDED' });
    await renderPage('Key Metrics');
    fireEvent.click(toolbarButton('Next item').getElement());
    await waitFor(() =>
      expect(state.app.setErrorMessage).toHaveBeenCalledWith('Other Annotators claimed the waiting items first. Try again.'),
    );
  });

  it('reports "Claimed 1 item." for a claim of two items of which one succeeded', async () => {
    await renderPage('Key Metrics');
    selectRow('a.pdf');
    selectRow('b.pdf');
    fireEvent.click(toolbarButton('Claim (2)').getElement());
    await waitFor(() => expect(state.app.setSuccessMessage).toHaveBeenCalledWith('Claimed 1 item. Work it in Home › Tasks.'));
    expect(state.app.setErrorMessage).toHaveBeenCalledWith(
      "Couldn't claim 1 item: b.pdf (Failed to claim it: INVALID_STATE: the item is not open)",
    );
  });
});
