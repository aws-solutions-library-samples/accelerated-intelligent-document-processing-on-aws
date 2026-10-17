// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React from 'react';
import { act, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { InboxContextValue } from '../../../contexts/inbox';
import type { InboxMessage } from '../../../graphql/generated/operation-types';

const { graphqlMock, inboxRef } = vi.hoisted(() => ({
  graphqlMock: vi.fn(),
  inboxRef: { current: {} as Partial<InboxContextValue> },
}));
vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: graphqlMock }) }));
vi.mock('../../../hooks/use-inbox', () => ({ default: () => inboxRef.current }));
vi.mock('../../../contexts/app', () => ({ default: () => ({ setSuccessMessage: vi.fn(), setErrorMessage: vi.fn() }) }));
vi.mock('../../common/SafeMarkdown', () => ({
  default: ({ children }: { children: string }) => <div data-testid="safe-markdown">{children}</div>,
}));

import InboxPage from '../InboxPage';
import { EMPTY_DESCRIPTION, EMPTY_TITLE } from '../InboxMessagesTable';

const msg = (id: string, minutesAgo: number, over: Partial<InboxMessage> = {}): InboxMessage => ({
  id,
  type: 'review.assigned',
  title: `Message ${id}`,
  body: `Body of ${id}`,
  severity: 'info',
  link: null,
  source: 'hitl',
  createdAt: new Date(Date.now() - minutesAgo * 60_000).toISOString(),
  read: false,
  readAt: null,
  metadata: null,
  ...over,
});

const M1 = msg('1', 1, { severity: 'warning', link: '/documents/a.pdf' });
const M2 = msg('2', 2, { read: true, readAt: '2026-10-10T10:00:00Z', type: 'review.expiring', link: 'https://example.com/x' });
const M3 = msg('3', 3, { severity: 'error' });

const TYPES = [
  {
    id: 'review.assigned',
    displayName: 'Review assigned',
    description: 'A review was assigned to you',
    defaultSeverity: 'info',
    muted: false,
  },
  {
    id: 'review.expiring',
    displayName: 'Claim expiring',
    description: 'A claim is about to expire',
    defaultSeverity: 'warning',
    muted: false,
  },
];

const listPage = (items: InboxMessage[]) => ({ data: { listInboxMessages: { items, nextToken: null, unreadCount: 2 } } });

const routeGraphql = (items: InboxMessage[]) => (args: { query: string }) => {
  if (args.query.includes('listInboxMessages(')) return Promise.resolve(listPage(items));
  if (args.query.includes('getInboxPreferences')) {
    return Promise.resolve({ data: { getInboxPreferences: { types: TYPES, mutedTypes: [], updatedAt: null } } });
  }
  return Promise.resolve({ data: {} });
};

const result = { updated: 1, notFoundIds: [], unreadCount: 1 };
const inbox = {
  markMessages: vi.fn(),
  markAllRead: vi.fn(),
  deleteMessages: vi.fn(),
  clearRead: vi.fn(),
  syncUnreadCount: vi.fn(),
};

const renderPage = (path = '/inbox') =>
  render(
    <MemoryRouter initialEntries={[path]}>
      <InboxPage />
    </MemoryRouter>,
  );

const wrapper = () => createWrapper(document.body);
const table = () => {
  const t = wrapper().findTable();
  if (!t) throw new Error('table not rendered');
  return t;
};
const rowTitles = () =>
  table()
    .findRows()
    .map((r) => r.findAll('button').map((b) => b.getElement().textContent)[0]);
const button = (name: string) => screen.getByRole('button', { name }) as HTMLButtonElement;
// Hidden modals keep their footers in the DOM, so header actions are looked up in the table header.
const headerButton = (name: string) => within(table().findHeaderSlot()!.getElement()).getByRole('button', { name }) as HTMLButtonElement;
// A SegmentedControl renders a Select for narrow viewports too; the filter Selects are the last two.
const filterSelects = () => wrapper().findAllSelects().slice(-2);
// "More actions" in the table header holds the list-wide actions.
const moreActions = () => wrapper().findTable()!.findHeaderSlot()!.findButtonDropdown()!;
const openMoreActions = () => act(() => moreActions().openDropdown());
// Columns: selection, Received, Title, Type, Severity, Action.
const SEVERITY_COLUMN = 5;
const ACTION_COLUMN = 6;
const rowMenu = (row: number) => table().findBodyCell(row, ACTION_COLUMN)!.findButtonDropdown()!;
const selectRow = (index: number) => act(() => table().findRowSelectionArea(index)!.click());

// Every modal stays mounted; the open one is the visible one.
const openModal = () => {
  const modal = wrapper()
    .findAllModals()
    .find((m) => m.isVisible());
  if (!modal) throw new Error('no open modal');
  return modal;
};

const loaded = async () => {
  await screen.findByText('Message 1');
};

describe('InboxPage', () => {
  beforeEach(() => {
    graphqlMock.mockReset().mockImplementation(routeGraphql([M1, M2, M3]));
    Object.values(inbox).forEach((fn) => fn.mockReset());
    inbox.markMessages.mockResolvedValue(result);
    inbox.markAllRead.mockResolvedValue(result);
    inbox.deleteMessages.mockResolvedValue(result);
    inbox.clearRead.mockResolvedValue(result);
    inboxRef.current = { unreadCount: 2, changeToken: 0, recent: [], loaded: true, ...inbox };
    localStorage.clear();
  });

  it('shows the loading state while messages load', async () => {
    graphqlMock.mockImplementation((args: { query: string }) =>
      args.query.includes('listInboxMessages(') ? new Promise(() => undefined) : routeGraphql([])(args),
    );
    renderPage();
    expect(await screen.findByText('Loading messages')).toBeTruthy();
  });

  it('shows the empty state with its exact text', async () => {
    graphqlMock.mockImplementation(routeGraphql([]));
    renderPage();
    expect(await screen.findByText(EMPTY_TITLE)).toBeTruthy();
    expect(screen.getByText(EMPTY_DESCRIPTION)).toBeTruthy();
    expect(EMPTY_DESCRIPTION).toBe("You'll see messages here when work is assigned to you or something needs your attention.");
    expect(inbox.syncUnreadCount).toHaveBeenCalledWith(2);
  });

  it('shows no-match with a working clear-filter action', async () => {
    renderPage();
    await loaded();
    act(() => table().findTextFilter()!.findInput().setInputValue('zzz-nothing'));
    expect(screen.getByText('No matches')).toBeTruthy();
    act(() => button('Clear filter').click());
    expect(rowTitles()).toEqual(['Message 1', 'Message 2', 'Message 3']);
  });

  it('shows an error Alert whose Retry reloads', async () => {
    graphqlMock.mockImplementation((args: { query: string }) =>
      args.query.includes('listInboxMessages(')
        ? Promise.reject({ errors: [{ message: 'down', errorType: 'InternalError' }] })
        : routeGraphql([])(args),
    );
    renderPage();
    expect(await screen.findByText('Could not load messages')).toBeTruthy();

    graphqlMock.mockImplementation(routeGraphql([M1]));
    act(() => button('Retry').click());
    await loaded();
    expect(screen.queryByText('Could not load messages')).toBeNull();
  });

  it('filters by read state, type and severity', async () => {
    renderPage();
    await loaded();
    await screen.findAllByText('Review assigned');

    act(() => wrapper().findSegmentedControl()!.findSegments()[1].click());
    expect(rowTitles()).toEqual(['Message 1', 'Message 3']);
    act(() => wrapper().findSegmentedControl()!.findSegments()[0].click());

    const [typeSelect, severitySelect] = filterSelects();
    act(() => typeSelect.openDropdown());
    act(() => typeSelect.selectOptionByValue('review.expiring'));
    expect(rowTitles()).toEqual(['Message 2']);
    act(() => typeSelect.openDropdown());
    act(() => typeSelect.selectOptionByValue(''));

    act(() => severitySelect.openDropdown());
    act(() => severitySelect.selectOptionByValue('error'));
    expect(rowTitles()).toEqual(['Message 3']);
  });

  it('labels the type filter options by display name', async () => {
    renderPage();
    await loaded();
    await screen.findAllByText('Review assigned');
    const [typeSelect] = filterSelects();
    act(() => typeSelect.openDropdown());
    const labels = typeSelect
      .findDropdown()
      .findOptions()
      .map((o) => o.getElement().textContent);
    expect(labels).toEqual(['All types', 'Review assigned', 'Claim expiring']);
  });

  it('disables the bulk actions with a reason when nothing is selected', async () => {
    renderPage();
    await loaded();
    for (const name of ['Mark as read', 'Mark as unread', 'Delete']) {
      expect(headerButton(name).disabled).toBe(true);
      expect(headerButton(name).closest('span[title]')?.getAttribute('title')).toBe('Select one or more messages');
    }
  });

  it('marks the selected messages read and unread through the provider', async () => {
    renderPage();
    await loaded();
    selectRow(1);
    selectRow(3);
    await act(async () => headerButton('Mark as read').click());
    expect(inbox.markMessages).toHaveBeenCalledWith([M1, M3], true);

    selectRow(2);
    await act(async () => headerButton('Mark as unread').click());
    expect(inbox.markMessages).toHaveBeenLastCalledWith([expect.objectContaining({ id: '2' })], false);
  });

  it('deletes via the confirmation modal and returns focus to the heading', async () => {
    renderPage();
    await loaded();
    selectRow(1);
    act(() => headerButton('Delete').click());
    const modal = openModal();
    expect(modal.getElement().textContent).toContain('Message 1');
    await act(async () => modal.findFooter()!.findButton('[class*="variant-primary"]')!.click());

    expect(inbox.deleteMessages).toHaveBeenCalledWith([M1]);
    await waitFor(() => expect(rowTitles()).toEqual(['Message 2', 'Message 3']));
    expect(document.activeElement?.textContent).toBe('Messages');
  });

  it('marks all read, and clears read messages after confirmation', async () => {
    renderPage();
    await loaded();
    openMoreActions();
    await act(async () => moreActions().findItemById('mark-all-read')!.click());
    expect(inbox.markAllRead).toHaveBeenCalledTimes(1);

    openMoreActions();
    act(() => moreActions().findItemById('clear-read')!.click());
    expect(inbox.clearRead).not.toHaveBeenCalled();
    const modal = openModal();
    await act(async () => modal.findFooter()!.findButton('[class*="variant-primary"]')!.click());
    expect(inbox.clearRead).toHaveBeenCalledTimes(1);
  });

  it('disables Mark all as read when nothing is unread', async () => {
    inboxRef.current = { ...inboxRef.current, unreadCount: 0 };
    renderPage();
    await loaded();
    openMoreActions();
    expect(moreActions().findItemById('mark-all-read', { disabled: true })).not.toBeNull();
    expect(moreActions().findItemById('clear-read', { disabled: false })).not.toBeNull();
  });

  it('offers per-row Open, Mark as read or unread, and Delete', async () => {
    renderPage();
    await loaded();
    const ids = (row: number) => {
      act(() => rowMenu(row).openDropdown());
      const found = rowMenu(row)
        .findItems()
        .map((i) => i.getElement().getAttribute('data-testid'));
      act(() => rowMenu(row).openDropdown());
      return found;
    };
    expect(ids(1)).toEqual(['open', 'read', 'delete']);
    expect(ids(2)).toEqual(['open', 'unread', 'delete']);

    act(() => rowMenu(1).openDropdown());
    await act(async () => rowMenu(1).findItemById('read')!.click());
    expect(inbox.markMessages).toHaveBeenCalledWith([M1], true);

    act(() => rowMenu(3).openDropdown());
    act(() => rowMenu(3).findItemById('delete')!.click());
    const modal = openModal();
    expect(modal.getElement().textContent).toContain('Message 3');
    await act(async () => modal.findFooter()!.findButton('[class*="variant-primary"]')!.click());
    expect(inbox.deleteMessages).toHaveBeenCalledWith([M3]);
  });

  it('sorts severity by rank, not alphabetically', async () => {
    renderPage();
    await loaded();
    act(() => table().findColumnSortingArea(SEVERITY_COLUMN)!.click());
    expect(rowTitles()).toEqual(['Message 2', 'Message 1', 'Message 3']);
    act(() => table().findColumnSortingArea(SEVERITY_COLUMN)!.click());
    expect(rowTitles()).toEqual(['Message 3', 'Message 1', 'Message 2']);
  });

  it('renders the body through SafeMarkdown, marks it read, and links internal pages only', async () => {
    renderPage();
    await loaded();

    act(() => button('Message 1').click());
    expect(screen.getByTestId('safe-markdown').textContent).toBe('Body of 1');
    expect(screen.getByText('Open related page').closest('a')?.getAttribute('href')).toBe('#/documents/a.pdf');
    await waitFor(() => expect(inbox.markMessages).toHaveBeenCalledWith([M1], true, { silent: true }));
    act(() => button('Close').click());

    act(() => button('Message 2').click());
    expect(screen.getByTestId('safe-markdown').textContent).toBe('Body of 2');
    expect(screen.queryByText('Open related page')).toBeNull();
    expect(inbox.markMessages).toHaveBeenCalledTimes(1);
  });

  it('opens Inbox settings from the ?tab=preferences deep link, and closing it keeps the messages', async () => {
    renderPage('/inbox?tab=preferences');
    expect(openModal().findHeader().getElement().textContent).toBe('Inbox settings');
    expect(await screen.findByText('A review was assigned to you')).toBeTruthy();
    await loaded();
    act(() => openModal().findDismissButton().click());
    expect(
      wrapper()
        .findAllModals()
        .some((m) => m.isVisible()),
    ).toBe(false);
    expect(rowTitles()).toEqual(['Message 1', 'Message 2', 'Message 3']);
  });

  it('opens Inbox settings from the header settings button', async () => {
    renderPage();
    await loaded();
    expect(
      wrapper()
        .findAllModals()
        .some((m) => m.isVisible()),
    ).toBe(false);
    act(() => button('Inbox settings').click());
    expect(openModal().findHeader().getElement().textContent).toBe('Inbox settings');
  });
});
