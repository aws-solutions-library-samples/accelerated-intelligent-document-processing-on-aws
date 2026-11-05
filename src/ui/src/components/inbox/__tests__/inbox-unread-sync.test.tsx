// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The Inbox page, the bell and the navigation badge under one real InboxProvider.
 * Mark all as read (and mark read) must drop every unread counter as soon as the
 * action resolves: no timer is advanced, and later list reads never resolve or are
 * stale, so only the action itself can have moved the counters.
 */
import React from 'react';
import { act, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { InboxMessage } from '../../../graphql/generated/operation-types';

const { graphqlMock } = vi.hoisted(() => ({ graphqlMock: vi.fn() }));
vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: graphqlMock }) }));
vi.mock('../../../contexts/app', () => ({ default: () => ({ setSuccessMessage: vi.fn(), setErrorMessage: vi.fn() }) }));
vi.mock('../../common/SafeMarkdown', () => ({
  default: ({ children }: { children: string }) => <div>{children}</div>,
}));

import { InboxProvider } from '../../../contexts/inbox';
import useInbox from '../../../hooks/use-inbox';
import InboxPage from '../InboxPage';
import { useInboxBellUtility } from '../inbox-bell';
import { buildInboxNavLink } from '../inbox-nav-link';

const msg = (id: string, read: boolean): InboxMessage => ({
  id,
  type: 'review.assigned',
  title: `Message ${id}`,
  body: '',
  severity: 'info',
  link: null,
  source: 'test',
  createdAt: new Date().toISOString(),
  read,
  readAt: read ? new Date().toISOString() : null,
  metadata: null,
});

const UNREAD = [msg('1', false), msg('2', false)];
const page = (items: InboxMessage[], unreadCount: number) => ({ data: { listInboxMessages: { items, nextToken: null, unreadCount } } });
const never = () => new Promise(() => undefined);

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (v: T) => void;
}
const deferred = <T,>(): Deferred<T> => {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
};

/** The bell's accessible label and the navigation badge text, as rendered. */
const CounterProbe = (): React.JSX.Element => {
  const bell = useInboxBellUtility();
  const { unreadCount } = useInbox();
  const nav = buildInboxNavLink(unreadCount);
  return (
    <div>
      <span data-testid="bell">{bell.ariaLabel}</span>
      <span data-testid="bell-badge">{String(bell.badge)}</span>
      <span data-testid="nav">{nav.info ?? 'no badge'}</span>
    </div>
  );
};

const renderAll = () =>
  render(
    <MemoryRouter initialEntries={['/inbox']}>
      <InboxProvider>
        <CounterProbe />
        <InboxPage />
      </InboxProvider>
    </MemoryRouter>,
  );

const text = (id: string) => screen.getByTestId(id).textContent;
const isList = (args: { query: string }) => args.query.includes('listInboxMessages(');
const moreActions = () => createWrapper(document.body).findTable()!.findHeaderSlot()!.findButtonDropdown()!;

const expectUnread = (n: number) => {
  if (n === 0) {
    expect(text('bell')).toBe('Inbox, no unread messages');
    expect(text('bell-badge')).toBe('false');
    expect(text('nav')).toBe('no badge');
  } else {
    expect(text('bell')).toBe(`Inbox, ${n} unread messages`);
    expect(text('bell-badge')).toBe('true');
    expect(text('nav')).toContain(String(n));
  }
};

describe('Inbox unread counters', () => {
  beforeEach(() => {
    graphqlMock.mockReset();
  });

  it('drops the bell and navigation counts to 0 as soon as Mark all as read resolves', async () => {
    let lists = 0;
    graphqlMock.mockImplementation((args: { query: string }) => {
      if (isList(args)) {
        lists += 1;
        // The first two reads (provider poll, page load) are the initial state; any
        // later read never answers, so only the mutation can move the counters.
        return lists <= 2 ? Promise.resolve(page(UNREAD, 2)) : never();
      }
      if (args.query.includes('markAllInboxMessagesRead')) {
        return Promise.resolve({ data: { markAllInboxMessagesRead: { updated: 2, notFoundIds: [], unreadCount: 0 } } });
      }
      return Promise.resolve({ data: { getInboxPreferences: { types: [], mutedTypes: [], updatedAt: null } } });
    });
    renderAll();
    await screen.findByText('Message 1');
    expectUnread(2);

    act(() => moreActions().openDropdown());
    await act(async () => moreActions().findItemById('mark-all-read')!.click());

    expectUnread(0);
  });

  it('keeps the counts at 0 when a list read that started before the action answers after it', async () => {
    const stale = deferred<ReturnType<typeof page>>();
    let pageLoads = 0;
    graphqlMock.mockImplementation((args: { query: string; variables?: { limit?: number } }) => {
      if (isList(args)) {
        // The provider's poll asks for the bell's 5; the page asks for 100 per page.
        if (args.variables?.limit !== 100) return pageLoads === 0 ? Promise.resolve(page(UNREAD, 2)) : never();
        pageLoads += 1;
        if (pageLoads === 1) return Promise.resolve(page(UNREAD, 2));
        if (pageLoads === 2) return stale.promise; // the Refresh below, still on the wire
        return Promise.resolve(page([msg('1', true), msg('2', true)], 0));
      }
      if (args.query.includes('markInboxMessages')) {
        return Promise.resolve({ data: { markInboxMessages: { updated: 2, notFoundIds: [], unreadCount: 0 } } });
      }
      return Promise.resolve({ data: { getInboxPreferences: { types: [], mutedTypes: [], updatedAt: null } } });
    });
    renderAll();
    await screen.findByText('Message 1');
    expectUnread(2);

    // A refresh goes out, then the user marks both rows read before it answers.
    act(() => screen.getByRole('button', { name: 'Refresh messages' }).click());
    const table = createWrapper(document.body).findTable()!;
    act(() => table.findSelectAllTrigger()!.click());
    await act(async () => screen.getByRole('button', { name: 'Mark as read' }).click());
    expectUnread(0);

    // The refresh answers with the pre-action state; it must not put 2 back.
    await act(async () => stale.resolve(page(UNREAD, 2)));
    expectUnread(0);
    expect(pageLoads).toBe(3);
  });
});
