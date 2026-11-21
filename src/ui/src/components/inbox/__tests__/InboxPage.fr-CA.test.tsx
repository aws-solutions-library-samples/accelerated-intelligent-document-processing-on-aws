// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The Inbox page and the Inbox bell under `fr-CA`: with a fixture catalog the heading, a
 * column header, the severity and Type labels, the read-state filter, "just now", the empty
 * Inbox and the delete dialog for one message and for three read French, and so does the
 * bell's screen-reader label for 0, 1 and 5 unread messages. The message list is requested
 * once, not again when the catalog arrives. Under `en` the same renders read today's English.
 * Message titles stay as the platform writes them.
 */
import React from 'react';
import { act, screen, waitFor, within } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { I18n } from 'aws-amplify/utils';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { InboxContextValue } from '../../../contexts/inbox';
import type { InboxMessage } from '../../../graphql/generated/operation-types';
import { renderHookWithIntl, renderWithIntl } from '../../../test-utils/renderWithIntl';

// One value per mocked hook for every render, so no render hands out a fresh object.
const state = vi.hoisted(() => ({
  graphql: vi.fn(),
  inbox: { current: {} as Partial<InboxContextValue> },
  app: { setSuccessMessage: vi.fn(), setErrorMessage: vi.fn() },
}));

vi.mock('virtual:i18n-catalogs', () => import('../../../test-utils/i18nCatalogsMock'));
vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: state.graphql }) }));
vi.mock('../../../hooks/use-inbox', () => ({ default: () => state.inbox.current }));
vi.mock('../../../contexts/app', () => ({ default: () => state.app }));
vi.mock('../../common/SafeMarkdown', () => ({
  default: ({ children }: { children: string }) => <div>{children}</div>,
}));

import InboxPage from '../InboxPage';
import { useInboxBellUtility } from '../inbox-bell';

const MINUTE = 60_000;

const msg = (id: string, createdAt: number, over: Partial<InboxMessage> = {}): InboxMessage => ({
  id,
  type: 'review.item_reassigned',
  title: `Message ${id}`,
  body: '',
  severity: 'warning',
  link: null,
  source: 'review_queues',
  createdAt: new Date(createdAt).toISOString(),
  read: false,
  readAt: null,
  metadata: null,
  ...over,
});

/** Newest first, as the table sorts them: a task warning from just now, then older queue news. */
const inboxMessages = (): InboxMessage[] => {
  const now = Date.now();
  return [
    msg('1', now),
    msg('2', now - 120 * MINUTE, { type: 'review.queue_items_waiting', severity: 'info' }),
    msg('3', now - 26 * 60 * MINUTE, { type: 'review.queue_items_waiting', severity: 'info', read: true }),
  ];
};

/** The message types, named as the platform writes them. */
const TYPES = [
  {
    id: 'review.item_reassigned',
    displayName: 'Review item assigned to you',
    description: 'An Admin assigned a review queue item or Test Studio document to you.',
    defaultSeverity: 'info',
    muted: false,
  },
  {
    id: 'review.queue_items_waiting',
    displayName: 'Review queue items waiting',
    description: 'A document is waiting in a review queue you work.',
    defaultSeverity: 'info',
    muted: false,
  },
];

const isList = (query: string) => query.includes('listInboxMessages(');

// Route each query by the operation name embedded in the query string.
const routeGraphql =
  (items: InboxMessage[]) =>
  ({ query }: { query: string }) => {
    if (isList(query)) return Promise.resolve({ data: { listInboxMessages: { items, nextToken: null, unreadCount: 2 } } });
    if (query.includes('getInboxPreferences')) {
      return Promise.resolve({ data: { getInboxPreferences: { types: TYPES, mutedTypes: [], updatedAt: null } } });
    }
    return Promise.resolve({ data: {} });
  };

const result = { updated: 1, notFoundIds: [], unreadCount: 1 };

/** The Inbox state the page and the bell read; `unreadCount` varies by test. */
const setInbox = (unreadCount: number | null) => {
  state.inbox.current = {
    unreadCount,
    recent: [],
    loaded: unreadCount != null,
    changeToken: 0,
    beginUnreadSync: () => () => true,
    markMessages: vi.fn().mockResolvedValue(result),
    markAllRead: vi.fn().mockResolvedValue(result),
    deleteMessages: vi.fn().mockResolvedValue(result),
    clearRead: vi.fn().mockResolvedValue(result),
  };
};

/** The strings the assertions read; the rest fall back to English, as the app's error policy allows. */
const FR = {
  'inbox.page.title': 'Boîte de réception',
  'inbox.messagesTable.column.received': 'Reçu',
  'inbox.messagesTable.column.severity': 'Gravité',
  'inbox.messagesTable.readState.label': 'État de lecture',
  'inbox.messagesTable.readState.all': 'Tous',
  'inbox.messagesTable.readState.unread': 'Non lus',
  'inbox.messagesTable.deleteSelected': 'Supprimer',
  'inbox.messagesTable.empty.title': 'Aucun message',
  'inbox.messagesTable.empty.body': 'Vos messages s’afficheront ici quand du travail vous sera assigné.',
  'inbox.severity.warning': 'Avertissement',
  'inbox.severity.info': 'Information',
  'inbox.category.task': 'Tâche',
  'inbox.category.queue': 'File',
  'inbox.deleteModal.header': '{count, plural, one {Supprimer le message} other {Supprimer les messages}}',
  'inbox.deleteModal.body':
    'Voulez-vous vraiment supprimer {count, plural, one {ce message} other {ces # messages}} ? Cette action est irréversible.',
  'common.relativeTime.justNow': 'à l’instant',
  'inbox.bell.ariaLabel':
    '{count, plural, =0 {Boîte de réception, aucun message non lu} one {Boîte de réception, # message non lu} other {Boîte de réception, # messages non lus}}',
};
const FR_CA = { locale: 'fr-CA', enabledLocales: ['en', 'fr-CA'], messages: FR } as const;

const Router = ({ children }: { children: React.ReactNode }) => <MemoryRouter initialEntries={['/inbox']}>{children}</MemoryRouter>;

const table = () => {
  const found = createWrapper(document.body).findTable();
  if (!found) throw new Error('table not rendered');
  return found;
};
// Columns: selection, Received, Title, Type, Severity, Action.
const RECEIVED = 2;
const TYPE = 4;
const SEVERITY = 5;
const ACTION = 6;
const cellText = (row: number, column: number) => table().findBodyCell(row, column)?.getElement().textContent;
const columnHeaders = () =>
  table()
    .findColumnHeaders()
    .map((h) => h.getElement().textContent);
const readStates = () =>
  createWrapper(document.body)
    .findSegmentedControl()!
    .findSegments()
    .map((s) => s.getElement().textContent);
// Hidden modals keep their footers in the DOM, so the bulk Delete is looked up in the table header.
const headerButton = (name: string) => within(table().findHeaderSlot()!.getElement()).getByRole('button', { name });
// Every modal stays mounted; the open one is the visible one.
const openModal = () => {
  const modal = createWrapper(document.body)
    .findAllModals()
    .find((m) => m.isVisible());
  if (!modal) throw new Error('no open modal');
  return modal;
};

/** The delete dialog's title and question, for row 3's menu (one message), then for all three. */
const deleteDialogs = (bulkDelete: string) => {
  const menu = table().findBodyCell(3, ACTION)!.findButtonDropdown()!;
  act(() => menu.openDropdown());
  act(() => menu.findItemById('delete')!.click());
  const one = { title: openModal().findHeader().getElement().textContent, body: openModal().findContent().getElement().textContent };
  act(() => openModal().findDismissButton().click());
  act(() => table().findSelectAllTrigger()!.click());
  act(() => headerButton(bulkDelete).click());
  const three = { title: openModal().findHeader().getElement().textContent, body: openModal().findContent().getElement().textContent };
  return { one, three };
};

const listCalls = () => state.graphql.mock.calls.filter(([arg]) => isList(String((arg as { query: string }).query)));

beforeEach(() => {
  state.graphql.mockReset();
  state.graphql.mockImplementation(routeGraphql(inboxMessages()));
  setInbox(2);
});

afterEach(() => {
  window.localStorage.clear();
  document.documentElement.lang = '';
  I18n.setLanguage('');
});

describe('the Inbox under fr-CA', () => {
  it('reads its heading, a column, the severity and Type labels, the read-state filter and "just now" in French', async () => {
    renderWithIntl(<InboxPage />, { ...FR_CA, wrapper: Router });
    // Awaited: the catalog arrives asynchronously, as the chunk does in the browser.
    expect(await screen.findByText('Boîte de réception')).toBeInTheDocument();
    expect(await screen.findByText('Message 1')).toBeInTheDocument();
    expect(columnHeaders()).toEqual(expect.arrayContaining(['Reçu', 'Gravité']));
    expect(columnHeaders()).not.toContain('Received');
    expect([cellText(1, SEVERITY), cellText(1, TYPE)]).toEqual(['Avertissement', 'Tâche']);
    expect([cellText(2, SEVERITY), cellText(2, TYPE)]).toEqual(['Information', 'File']);
    expect(cellText(1, RECEIVED)).toBe('à l’instant');
    expect(readStates()).toEqual(['Tous', 'Non lus']);
    expect(screen.getAllByLabelText('État de lecture').length).toBeGreaterThan(0);
    // The type's own name, the Type cell's hover title, is the platform's text in every language.
    await waitFor(() =>
      expect(table().findBodyCell(1, TYPE)!.find('span[title]')!.getElement().getAttribute('title')).toBe('Review item assigned to you'),
    );
  });

  it('asks to confirm deleting one message, and three, in French', async () => {
    renderWithIntl(<InboxPage />, { ...FR_CA, wrapper: Router });
    expect(await screen.findByText('Boîte de réception')).toBeInTheDocument();
    expect(await screen.findByText('Message 1')).toBeInTheDocument();
    const { one, three } = deleteDialogs('Supprimer');
    expect(one.title).toBe('Supprimer le message');
    expect(one.body).toContain('Voulez-vous vraiment supprimer ce message ? Cette action est irréversible.');
    expect(three.title).toBe('Supprimer les messages');
    expect(three.body).toContain('Voulez-vous vraiment supprimer ces 3 messages ? Cette action est irréversible.');
  });

  it('shows an empty Inbox in French', async () => {
    state.graphql.mockImplementation(routeGraphql([]));
    renderWithIntl(<InboxPage />, { ...FR_CA, wrapper: Router });
    expect(await screen.findByText('Aucun message')).toBeInTheDocument();
    expect(screen.getByText('Vos messages s’afficheront ici quand du travail vous sera assigné.')).toBeInTheDocument();
    expect(screen.queryByText('No messages')).toBeNull();
  });

  // The catalog arrives after the first render, as a new `intl`; that must not load the list again.
  it('requests the message list once, not again when the catalog arrives', async () => {
    renderWithIntl(<InboxPage />, { ...FR_CA, wrapper: Router });
    expect(await screen.findByText('Boîte de réception')).toBeInTheDocument();
    expect(await screen.findByText('Message 1')).toBeInTheDocument();
    expect(listCalls()).toHaveLength(1);
  });

  it.each([
    [0, 'Boîte de réception, aucun message non lu'],
    [1, 'Boîte de réception, 1 message non lu'],
    [5, 'Boîte de réception, 5 messages non lus'],
  ])('labels the bell for %s unread messages in French', async (count, label) => {
    setInbox(count);
    const { result: bell } = renderHookWithIntl(() => useInboxBellUtility(), FR_CA);
    await waitFor(() => expect(bell.current.ariaLabel).toBe(label));
  });
});

describe('the Inbox under en', () => {
  it('reads the inline English for its heading, a column, the severity and Type labels, the read-state filter and "just now"', async () => {
    renderWithIntl(<InboxPage />, { wrapper: Router });
    expect(await screen.findByText('Message 1')).toBeInTheDocument();
    expect(screen.getByText('Inbox')).toBeInTheDocument();
    expect(columnHeaders()).toEqual(expect.arrayContaining(['Received', 'Severity']));
    expect([cellText(1, SEVERITY), cellText(1, TYPE)]).toEqual(['Warning', 'Task']);
    expect([cellText(2, SEVERITY), cellText(2, TYPE)]).toEqual(['Info', 'Queue']);
    expect(cellText(1, RECEIVED)).toBe('just now');
    expect(readStates()).toEqual(['All', 'Unread']);
    expect(screen.getAllByLabelText('Read state').length).toBeGreaterThan(0);
  });

  it('asks to confirm deleting one message, and three, in English', async () => {
    renderWithIntl(<InboxPage />, { wrapper: Router });
    expect(await screen.findByText('Message 1')).toBeInTheDocument();
    const { one, three } = deleteDialogs('Delete');
    expect(one.title).toBe('Delete message');
    expect(one.body).toContain('Are you sure you want to delete this message? This action cannot be undone.');
    expect(three.title).toBe('Delete messages');
    expect(three.body).toContain('Are you sure you want to delete these 3 messages? This action cannot be undone.');
  });

  it('shows an empty Inbox in English', async () => {
    state.graphql.mockImplementation(routeGraphql([]));
    renderWithIntl(<InboxPage />, { wrapper: Router });
    expect(await screen.findByText('No messages')).toBeInTheDocument();
    expect(
      screen.getByText("You'll see messages here when work is assigned to you or something needs your attention."),
    ).toBeInTheDocument();
  });

  it.each([
    [0, 'Inbox, no unread messages'],
    [1, 'Inbox, 1 unread message'],
    [5, 'Inbox, 5 unread messages'],
  ])('labels the bell for %s unread messages in English', (count, label) => {
    setInbox(count);
    const { result: bell } = renderHookWithIntl(() => useInboxBellUtility());
    expect(bell.current.ariaLabel).toBe(label);
  });
});
