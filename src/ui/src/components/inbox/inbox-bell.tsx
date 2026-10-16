// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The Inbox bell in the top navigation. It reads the shared Inbox poll, so it adds
// no request of its own: the badge, the label and the recent messages all come
// from the provider.
import type { ButtonDropdownProps, TopNavigationProps } from '@cloudscape-design/components';

import useInbox from '../../hooks/use-inbox';
import { formatRelativeTime } from '../../utils/date-time';
import { inboxHref, isInternalRoute } from '../../routes/constants';
import { severityIcon, severityLabel } from './severity';

const MSG_PREFIX = 'msg:';

export const inboxBellAriaLabel = (unreadCount: number | null): string => {
  if (unreadCount == null) return 'Inbox';
  if (unreadCount === 0) return 'Inbox, no unread messages';
  if (unreadCount === 1) return 'Inbox, 1 unread message';
  return `Inbox, ${unreadCount} unread messages`;
};

export const useInboxBellUtility = (): TopNavigationProps.MenuDropdownUtility => {
  const { unreadCount, recent, loaded, markMessages, markAllRead } = useInbox();

  const recentItems: ButtonDropdownProps.Item[] = recent.length
    ? recent.map((m) => ({
        id: `${MSG_PREFIX}${m.id}`,
        text: m.title,
        secondaryText: [severityLabel(m.severity), formatRelativeTime(m.createdAt), m.read ? null : 'Unread'].filter(Boolean).join(' · '),
        iconName: severityIcon(m.severity),
        href: isInternalRoute(m.link) ? `#${m.link}` : inboxHref(),
      }))
    : [{ id: 'recent-empty', text: loaded ? 'No messages yet' : 'Loading…', disabled: true }];

  const onItemClick: TopNavigationProps.MenuDropdownUtility['onItemClick'] = ({ detail }) => {
    if (detail.id.startsWith(MSG_PREFIX)) {
      const message = recent.find((m) => `${MSG_PREFIX}${m.id}` === detail.id);
      // No preventDefault: the item's href navigates. Marking is best-effort and
      // the provider already reports a failure, so the rejection is not rethrown.
      if (message && !message.read) markMessages([message], true, { silent: true }).catch(() => undefined);
      return;
    }
    if (detail.id === 'mark-all-read') markAllRead().catch(() => undefined);
  };

  return {
    type: 'menu-dropdown',
    iconName: 'notification',
    title: 'Inbox',
    ariaLabel: inboxBellAriaLabel(unreadCount),
    badge: (unreadCount ?? 0) > 0,
    items: [
      { text: 'Recent', items: recentItems },
      {
        items: [
          { id: 'mark-all-read', text: 'Mark all as read', disabled: !unreadCount, disabledReason: 'No unread messages' },
          { id: 'view-all', text: 'View all in Inbox', href: inboxHref() },
        ],
      },
    ],
    onItemClick,
  };
};

export default useInboxBellUtility;
