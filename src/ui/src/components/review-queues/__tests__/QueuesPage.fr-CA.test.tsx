// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Review Studio › Review Management, its queue Settings dialog and its help panel under
 * `fr-CA`. With a fixture catalog the heading, the Production | Test Studio switch, a column
 * header, the Default queue's disabled Delete reason and the delete confirmation read French,
 * and so do the Settings dialog's title and a field, and the help panel's heading and a bold
 * name. The queue lists are requested once, not again when the catalog arrives. Under `en`
 * the same renders read today's English. Queue names and ids stay as stored.
 */
import React from 'react';
import { act, screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import type { ButtonDropdownWrapper, ModalWrapper } from '@cloudscape-design/components/test-utils/dom';
import { I18n } from 'aws-amplify/utils';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { renderWithIntl } from '../../../test-utils/renderWithIntl';

// One value per mocked hook and call for every render, so no render hands out a fresh object.
const state = vi.hoisted(() => ({
  listReviewQueues: vi.fn(),
  fetchQueuesOverview: vi.fn(),
  listTestSetQueues: vi.fn(),
  listAnnotatorEmails: vi.fn(),
  app: { setSuccessMessage: vi.fn(), setErrorMessage: vi.fn() },
}));

vi.mock('virtual:i18n-catalogs', () => import('../../../test-utils/i18nCatalogsMock'));
vi.mock('../reviewer-assignment', () => ({
  listReviewQueues: () => state.listReviewQueues(),
  deleteReviewQueue: vi.fn(),
  listAnnotatorEmails: () => state.listAnnotatorEmails(),
  listTestSetQueues: () => state.listTestSetQueues(),
  createReviewQueue: vi.fn(),
  updateReviewQueue: vi.fn(),
  updateTestSetQueue: vi.fn(),
}));
vi.mock('../review-api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../review-api')>()),
  fetchQueuesOverview: () => state.fetchQueuesOverview(),
}));
vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: vi.fn() }) }));
vi.mock('../../../contexts/app', () => ({ default: () => state.app }));

import QueuesPage from '../QueuesPage';
import { ReviewManagementToolsPanel } from '../tools-panel';

const def = (id: string, over: Record<string, unknown> = {}) => ({
  id,
  name: id[0].toUpperCase() + id.slice(1),
  description: '',
  priority: 0,
  checklist: [],
  rejectReasons: [],
  skipReasons: [],
  timeLimitMinutes: 60,
  eligibleAnnotators: [],
  isDefaultQueue: id === 'default',
  enabled: true,
  ...over,
});

const health = (name: string, depth: number, claimed = 0) => ({
  reviewers: [],
  summary: { name, displayName: name, depth, unclaimed: depth - claimed, claimed, inProgress: 0 },
});

beforeEach(() => {
  state.listReviewQueues.mockReset().mockResolvedValue([def('default'), def('claims', { priority: 5 })]);
  state.fetchQueuesOverview.mockReset().mockResolvedValue({ queues: [health('default', 0), health('claims', 3, 1)] });
  state.listTestSetQueues.mockReset().mockResolvedValue([]);
  state.listAnnotatorEmails.mockReset().mockResolvedValue([]);
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
  'reviewQueues.queuesPage.title': 'Gestion des révisions',
  'reviewQueues.queuesPage.view.label': 'Files à afficher',
  'reviewQueues.queuesPage.view.production': 'Production',
  'reviewQueues.queuesPage.view.testStudio': 'Studio de test',
  'reviewQueues.queuesPage.column.queue': 'File',
  'reviewQueues.queuesPage.column.pending': 'En attente',
  'reviewQueues.queuesPage.production.title': 'Files de production',
  'reviewQueues.queuesPage.rowActions': 'Actions pour {name}',
  'reviewQueues.queuesPage.defaultNotDeletable': 'La file par défaut ne peut pas être supprimée',
  'reviewQueues.queuesPage.deleteDialog.title': 'Supprimer la file',
  'reviewQueues.queuesPage.confirmDeleteMoving':
    'Supprimer la file {name} ({id}) ? Cette action est irréversible. {count, plural, one {# élément sera déplacé} other {# éléments seront déplacés}} vers la file Default.',
  'reviewQueues.queueSettings.settingsTitle': 'Paramètres de la file : {name}',
  'reviewQueues.queueSettings.priority.label': 'Priorité',
  'reviewQueues.toolsPanel.managementTitle': 'Gestion des révisions',
  'reviewQueues.toolsPanel.managementIntro':
    'Toutes les files de révision au même endroit. Le sélecteur <b>Production | Studio de test</b> choisit le tableau affiché.',
};
const FR_CA = { locale: 'fr-CA', enabledLocales: ['en', 'fr-CA'], messages: FR } as const;

const Router = ({ children }: { children: React.ReactNode }) => <MemoryRouter>{children}</MemoryRouter>;

const table = () => {
  const found = createWrapper(document.body).findTable();
  if (!found) throw new Error('table not rendered');
  return found;
};
const tableTitle = () => table().findHeaderSlot()?.findHeader()?.findHeadingText().getElement().textContent;
const columnHeaders = () =>
  table()
    .findColumnHeaders()
    .map((h) => h.getElement().textContent);
const segments = () =>
  createWrapper(document.body)
    .findSegmentedControl()!
    .findSegments()
    .map((s) => s.getElement().textContent);
/** The page once its heading is French (or English) and both queues are listed. */
const renderPage = async (heading: string, options: Parameters<typeof renderWithIntl>[1] = {}) => {
  renderWithIntl(<QueuesPage />, { ...options, wrapper: Router });
  // Awaited: the catalog arrives asynchronously, as the chunk does in the browser.
  expect(await screen.findByRole('heading', { level: 1, name: heading })).toBeInTheDocument();
  await waitFor(() => expect(table().findRows()).toHaveLength(2));
};
const openMenu = (label: string): ButtonDropdownWrapper => {
  const menu = createWrapper(document.body)
    .findAllButtonDropdowns()
    .find((d) => d.findNativeButton().getElement().getAttribute('aria-label') === label);
  if (!menu) throw new Error(`no menu labelled ${label}`);
  act(() => menu.openDropdown());
  return menu;
};
/** Opens the menu labelled `label` and chooses its item `id`. */
const chooseMenuItem = (label: string, id: string) => {
  const menu = openMenu(label);
  act(() => menu.findItemById(id)!.click());
};
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
      .find((m) => m.findHeader().getElement().textContent === header);
    if (!found) throw new Error(`no dialog titled ${header}`);
    return found;
  });
const fieldLabels = (modal: ModalWrapper) =>
  createWrapper(modal.findContent().getElement())
    .findAllFormFields()
    .map((f) => f.findLabel()?.getElement().textContent);

describe('Review Management under fr-CA', () => {
  it("reads its heading, the switch, a column header and the Default queue's Delete reason in French", async () => {
    await renderPage('Gestion des révisions', FR_CA);
    expect(screen.getByRole('toolbar', { name: 'Files à afficher' })).toBeInTheDocument();
    expect(segments()).toEqual(['Production', 'Studio de test']);
    expect(tableTitle()).toBe('Files de production');
    expect(columnHeaders()).toEqual(expect.arrayContaining(['File', 'En attente']));
    expect(columnHeaders()).not.toContain('Queue');
    // The queue's name stays as stored; the reason its Delete is disabled reads French.
    expect(disabledReason(openMenu('Actions pour Default'), 'delete')).toBe('La file par défaut ne peut pas être supprimée');
  });

  it('asks in French before deleting a queue that holds items, naming how many move', async () => {
    await renderPage('Gestion des révisions', FR_CA);
    chooseMenuItem('Actions pour Claims', 'delete');
    const dialog = await findModal('Supprimer la file');
    expect(dialog.findContent().getElement().textContent).toBe(
      'Supprimer la file Claims (claims) ? Cette action est irréversible. 3 éléments seront déplacés vers la file Default.',
    );
  });

  it("titles the Default queue's Settings dialog and labels a field in French", async () => {
    await renderPage('Gestion des révisions', FR_CA);
    chooseMenuItem('Actions pour Default', 'edit');
    const dialog = await findModal('Paramètres de la file : Default');
    expect(fieldLabels(dialog)).toContain('Priorité');
    expect(fieldLabels(dialog)).not.toContain('Priority');
  });

  // The catalog arrives after the first render, as a new `intl`; that must not load the lists again.
  it('requests the queue lists once, not again when the catalog arrives', async () => {
    await renderPage('Gestion des révisions', FR_CA);
    expect(state.listReviewQueues).toHaveBeenCalledTimes(1);
    expect(state.fetchQueuesOverview).toHaveBeenCalledTimes(1);
    expect(state.listTestSetQueues).not.toHaveBeenCalled();
  });
});

describe('the Review Management help panel under fr-CA', () => {
  it('reads its heading and a bold name in French', async () => {
    renderWithIntl(<ReviewManagementToolsPanel />, FR_CA);
    expect(await screen.findByRole('heading', { level: 2, name: 'Gestion des révisions' })).toBeInTheDocument();
    expect(screen.getByText('Production | Studio de test').tagName).toBe('B');
    expect(screen.queryByText('Review Management')).toBeNull();
  });
});

describe('Review Management under en', () => {
  it("reads the inline English for its heading, the switch, a column header and the Default queue's Delete reason", async () => {
    await renderPage('Review Management');
    expect(screen.getByRole('toolbar', { name: 'Queues to show' })).toBeInTheDocument();
    expect(segments()).toEqual(['Production', 'Test Studio']);
    expect(tableTitle()).toBe('Production Queues');
    expect(columnHeaders()).toEqual(['Queue', 'Items', 'Pending', 'Assigned', 'Annotators', 'Actions']);
    expect(disabledReason(openMenu('Actions for Default'), 'delete')).toBe("The default queue can't be deleted");
    expect(state.listReviewQueues).toHaveBeenCalledTimes(1);
    expect(state.fetchQueuesOverview).toHaveBeenCalledTimes(1);
  });

  it('asks before deleting a queue that holds items, naming how many move', async () => {
    await renderPage('Review Management');
    chooseMenuItem('Actions for Claims', 'delete');
    const dialog = await findModal('Delete queue');
    expect(dialog.findContent().getElement().textContent).toBe(
      "Delete the queue Claims (claims)? This can't be undone. 3 items will move to the Default queue.",
    );
  });

  it("titles the Default queue's Settings dialog and labels a field", async () => {
    await renderPage('Review Management');
    chooseMenuItem('Actions for Default', 'edit');
    const dialog = await findModal('Queue Settings: Default');
    expect(fieldLabels(dialog)).toContain('Priority');
  });
});

describe('the Review Management help panel under en', () => {
  it('reads its heading and a bold name in English', () => {
    renderWithIntl(<ReviewManagementToolsPanel />);
    expect(screen.getByRole('heading', { level: 2, name: 'Review Management' })).toBeInTheDocument();
    expect(screen.getByText('Production | Test Studio').tagName).toBe('B');
  });
});
