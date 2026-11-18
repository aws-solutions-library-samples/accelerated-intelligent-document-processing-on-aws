// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import React from 'react';
import { Badge } from '@cloudscape-design/components';
import type { SideNavigationProps } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, type IntlShape, type NoMessageValues } from 'react-intl';

import { inboxHref } from '../../routes/constants';

const messages = defineMessages<{ text: NoMessageValues; unread: { readonly count: number } }>({
  text: {
    id: 'inbox.navLink.text',
    defaultMessage: 'Inbox',
    description: 'Side-navigation link, first in the Home section of every user, to the Inbox of notifications.',
  },
  unread: {
    id: 'inbox.navLink.unread',
    defaultMessage: '{count, plural, one {unread} other {unread}}',
    description:
      'Read by screen readers after the count badge of the Inbox link, as in "3 unread". {count} is the number of unread messages; the badge itself shows the number.',
  },
});

/** The Inbox entry at the top of the left navigation, with an unread badge. */
export const buildInboxNavLink = (
  unreadCount: number | null,
  /** Formats the label; without it (callers outside the navigation, tests) the label is the English source text. */
  intl?: Pick<IntlShape, 'formatMessage'>,
): SideNavigationProps.Link => {
  const link: SideNavigationProps.Link = {
    type: 'link',
    text: intl ? intl.formatMessage(messages.text) : String(messages.text.defaultMessage),
    href: inboxHref(),
  };
  const n = unreadCount ?? 0;
  if (n > 0) {
    link.info = (
      <span>
        <Badge color="blue">{n > 99 ? '99+' : n}</Badge>
        <span className="idp-visually-hidden">
          {' '}
          <FormattedMessage {...messages.unread} values={{ count: n }} />
        </span>
      </span>
    );
  }
  return link;
};

export default buildInboxNavLink;
