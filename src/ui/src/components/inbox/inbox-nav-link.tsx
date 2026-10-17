// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import React from 'react';
import { Badge } from '@cloudscape-design/components';
import type { SideNavigationProps } from '@cloudscape-design/components';

import { inboxHref } from '../../routes/constants';

/** The Inbox entry at the top of the left navigation, with an unread badge. */
export const buildInboxNavLink = (unreadCount: number | null): SideNavigationProps.Link => {
  const link: SideNavigationProps.Link = { type: 'link', text: 'Inbox', href: inboxHref() };
  const n = unreadCount ?? 0;
  if (n > 0) {
    link.info = (
      <span>
        <Badge color="blue">{n > 99 ? '99+' : n}</Badge>
        <span className="idp-visually-hidden"> unread</span>
      </span>
    );
  }
  return link;
};

export default buildInboxNavLink;
