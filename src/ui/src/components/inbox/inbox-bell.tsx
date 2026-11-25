// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The Inbox bell in the top navigation. It reads the shared Inbox poll, so it adds
// no request of its own: the badge, the label and the recent messages all come
// from the provider.
import type { ButtonDropdownProps, TopNavigationProps } from '@cloudscape-design/components';
import { createIntl, defineMessages, useIntl, type IntlShape, type NoMessageValues } from 'react-intl';

import useInbox from '../../hooks/use-inbox';
import { useFormatters } from '../../i18n/format';
import { formatRelativeTime, relativeTimeMessages } from '../../utils/date-time';
import { inboxHref, isInternalRoute } from '../../routes/constants';
import { severityIcon, severityLabel } from './severity';

const MSG_PREFIX = 'msg:';

const messages = defineMessages<{
  title: NoMessageValues;
  ariaLabel: { readonly count: number };
  recent: NoMessageValues;
  empty: NoMessageValues;
  loading: NoMessageValues;
  unread: NoMessageValues;
  markAllRead: NoMessageValues;
  noUnread: NoMessageValues;
  viewAll: NoMessageValues;
}>({
  title: {
    id: 'inbox.bell.title',
    defaultMessage: 'Inbox',
    description:
      "Title of the Inbox bell's menu in the top bar, and the bell's screen-reader label while the unread count is still loading.",
  },
  ariaLabel: {
    id: 'inbox.bell.ariaLabel',
    defaultMessage: '{count, plural, =0 {Inbox, no unread messages} one {Inbox, # unread message} other {Inbox, # unread messages}}',
    description: 'Screen-reader label of the Inbox bell in the top bar. {count} is the number of unread messages.',
  },
  recent: {
    id: 'inbox.bell.recent',
    defaultMessage: 'Recent',
    description: "Heading of the Inbox bell's menu, above the newest messages.",
  },
  empty: {
    id: 'inbox.bell.empty',
    defaultMessage: 'No messages yet',
    description: "Disabled item of the Inbox bell's menu when the Inbox is empty.",
  },
  loading: {
    id: 'inbox.bell.loading',
    defaultMessage: 'Loading…',
    description: "Disabled item of the Inbox bell's menu while the messages load.",
  },
  unread: {
    id: 'inbox.bell.unread',
    defaultMessage: 'Unread',
    description: "Marker of an unread message in the Inbox bell's menu, after its severity and its time.",
  },
  markAllRead: {
    id: 'inbox.bell.markAllRead',
    defaultMessage: 'Mark all as read',
    description: "Item of the Inbox bell's menu that marks every message as read.",
  },
  noUnread: {
    id: 'inbox.bell.noUnread',
    defaultMessage: 'No unread messages',
    description: 'Why Mark all as read is disabled in the Inbox bell menu: nothing is unread.',
  },
  viewAll: {
    id: 'inbox.bell.viewAll',
    defaultMessage: 'View all in Inbox',
    description: "Item of the Inbox bell's menu that opens the Inbox page.",
  },
});

/** Formats the English source text for callers that pass no `intl`. */
const englishIntl = createIntl({ locale: 'en', defaultLocale: 'en' });

/** The bell's screen-reader label in the UI's language; without `intl`, the English source text. */
export const inboxBellAriaLabel = (unreadCount: number | null, intl?: IntlShape): string => {
  const format = intl ?? englishIntl;
  return unreadCount == null ? format.formatMessage(messages.title) : format.formatMessage(messages.ariaLabel, { count: unreadCount });
};

export const useInboxBellUtility = (): TopNavigationProps.MenuDropdownUtility => {
  const intl = useIntl();
  const { unreadCount, recent, loaded, markMessages, markAllRead } = useInbox();
  const { locale } = useFormatters();

  const recentItems: ButtonDropdownProps.Item[] = recent.length
    ? recent.map((m) => ({
        id: `${MSG_PREFIX}${m.id}`,
        text: m.title,
        secondaryText: [
          severityLabel(m.severity, intl),
          formatRelativeTime(m.createdAt, locale, undefined, intl.formatMessage(relativeTimeMessages.justNow)),
          m.read ? null : intl.formatMessage(messages.unread),
        ]
          .filter(Boolean)
          .join(' · '),
        iconName: severityIcon(m.severity),
        href: isInternalRoute(m.link) ? `#${m.link}` : inboxHref(),
      }))
    : [{ id: 'recent-empty', text: intl.formatMessage(loaded ? messages.empty : messages.loading), disabled: true }];

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
    title: intl.formatMessage(messages.title),
    ariaLabel: inboxBellAriaLabel(unreadCount, intl),
    badge: (unreadCount ?? 0) > 0,
    items: [
      { text: intl.formatMessage(messages.recent), items: recentItems },
      {
        items: [
          {
            id: 'mark-all-read',
            text: intl.formatMessage(messages.markAllRead),
            disabled: !unreadCount,
            disabledReason: intl.formatMessage(messages.noUnread),
          },
          { id: 'view-all', text: intl.formatMessage(messages.viewAll), href: inboxHref() },
        ],
      },
    ],
    onItemClick,
  };
};

export default useInboxBellUtility;
