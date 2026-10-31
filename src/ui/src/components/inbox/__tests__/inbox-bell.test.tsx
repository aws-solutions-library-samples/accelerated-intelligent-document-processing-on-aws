// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React from 'react';
import { render, renderHook, screen } from '@testing-library/react';
import { TopNavigation } from '@cloudscape-design/components';
import type { ButtonDropdownProps } from '@cloudscape-design/components';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { InboxContextValue } from '../../../contexts/inbox';
import type { InboxMessage } from '../../../graphql/generated/operation-types';

const inboxRef = vi.hoisted(() => ({ current: {} as Partial<InboxContextValue> }));
vi.mock('../../../hooks/use-inbox', () => ({ default: () => inboxRef.current }));

import { useInboxBellUtility } from '../inbox-bell';

const NOW = Date.now();

const msg = (id: string, over: Partial<InboxMessage> = {}): InboxMessage => ({
  id,
  type: 'review.assigned',
  title: `Message ${id}`,
  body: '',
  severity: 'warning',
  link: '/documents/a.pdf',
  source: 'test',
  createdAt: new Date(NOW - 5 * 60_000).toISOString(),
  read: false,
  readAt: null,
  metadata: null,
  ...over,
});

const markMessages = vi.fn();
const markAllRead = vi.fn();

const setInbox = (over: Partial<InboxContextValue>) => {
  inboxRef.current = { unreadCount: 0, recent: [], loaded: true, markMessages, markAllRead, ...over };
};

const utility = () => renderHook(() => useInboxBellUtility()).result.current;

const groups = (items: ButtonDropdownProps.Items) => items as ReadonlyArray<ButtonDropdownProps.ItemGroup>;
const recentItems = (items: ButtonDropdownProps.Items) => groups(items)[0].items as ReadonlyArray<ButtonDropdownProps.Item>;
const actionItems = (items: ButtonDropdownProps.Items) => groups(items)[1].items as ReadonlyArray<ButtonDropdownProps.Item>;

type ItemClickEvent = Parameters<NonNullable<ReturnType<typeof utility>['onItemClick']>>[0];

const click = (u: ReturnType<typeof utility>, id: string): void => {
  u.onItemClick?.(new CustomEvent('click', { detail: { id } }) as unknown as ItemClickEvent);
};

describe('useInboxBellUtility', () => {
  beforeEach(() => {
    markMessages.mockReset().mockResolvedValue({ updated: 1, notFoundIds: [], unreadCount: 0 });
    markAllRead.mockReset().mockResolvedValue({ updated: 1, notFoundIds: [], unreadCount: 0 });
  });

  it.each([
    [null, 'Inbox'],
    [0, 'Inbox, no unread messages'],
    [1, 'Inbox, 1 unread message'],
    [3, 'Inbox, 3 unread messages'],
  ])('labels the bell for unreadCount %s', (count, label) => {
    setInbox({ unreadCount: count, loaded: count != null });
    expect(utility().ariaLabel).toBe(label);
  });

  it('shows the badge only when something is unread', () => {
    setInbox({ unreadCount: 0 });
    expect(utility().badge).toBe(false);
    setInbox({ unreadCount: null, loaded: false });
    expect(utility().badge).toBe(false);
    setInbox({ unreadCount: 2 });
    expect(utility().badge).toBe(true);
  });

  it('maps recent messages with severity, time and unread in that order', () => {
    setInbox({ unreadCount: 1, recent: [msg('1'), msg('2', { read: true, severity: 'error', link: null })] });
    const [first, second] = recentItems(utility().items);
    expect(first).toMatchObject({
      id: 'msg:1',
      text: 'Message 1',
      secondaryText: 'Warning · 5 minutes ago · Unread',
      iconName: 'status-warning',
      href: '#/documents/a.pdf',
    });
    expect(second.secondaryText).toBe('Error · 5 minutes ago');
    expect(second.iconName).toBe('status-negative');
  });

  it.each(['https://example.com/x', '//evil.example', 'javascript:alert(1)', '/a b', null])(
    'falls back to the Inbox for link %s',
    (link) => {
      setInbox({ unreadCount: 1, recent: [msg('1', { link })] });
      expect(recentItems(utility().items)[0].href).toBe('#/inbox');
    },
  );

  it('shows a single disabled placeholder when there are no messages', () => {
    setInbox({ unreadCount: null, loaded: false });
    expect(recentItems(utility().items)).toEqual([expect.objectContaining({ text: 'Loading…', disabled: true })]);
    setInbox({ unreadCount: 0, loaded: true });
    expect(recentItems(utility().items)).toEqual([expect.objectContaining({ text: 'No messages yet', disabled: true })]);
  });

  it('disables Mark all as read with a reason at zero unread', () => {
    setInbox({ unreadCount: 0 });
    const [markAll, viewAll] = actionItems(utility().items);
    expect(markAll).toMatchObject({ id: 'mark-all-read', disabled: true, disabledReason: 'No unread messages' });
    expect(viewAll).toMatchObject({ id: 'view-all', text: 'View all in Inbox', href: '#/inbox' });
    setInbox({ unreadCount: 4 });
    expect(actionItems(utility().items)[0].disabled).toBe(false);
  });

  it('marks an unread message read silently on click, and leaves a read one alone', () => {
    const unread = msg('1');
    const read = msg('2', { read: true });
    setInbox({ unreadCount: 1, recent: [unread, read] });
    const u = utility();

    click(u, 'msg:1');
    expect(markMessages).toHaveBeenCalledWith([unread], true, { silent: true });

    markMessages.mockClear();
    click(u, 'msg:2');
    expect(markMessages).not.toHaveBeenCalled();

    click(u, 'mark-all-read');
    expect(markAllRead).toHaveBeenCalledTimes(1);
  });

  it('renders in the top navigation with its accessible label', () => {
    setInbox({ unreadCount: 3, recent: [msg('1')] });
    const Harness = (): React.JSX.Element => {
      const inbox = useInboxBellUtility();
      return <TopNavigation identity={{ href: '#', title: 'Console' }} utilities={[inbox]} />;
    };
    render(<Harness />);
    expect(screen.getAllByLabelText('Inbox, 3 unread messages').length).toBeGreaterThan(0);
  });
});
