// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Its own AppLayout rather than GenAIIDPLayout, which mounts the document list and
// its poll. The Flashbar is the app's shared one, so action results show here too.
import React, { useState } from 'react';
import { AppLayout, BreadcrumbGroup, Flashbar } from '@cloudscape-design/components';
import { defineMessages, useIntl, type NoMessageValues } from 'react-intl';

import useAppContext from '../../contexts/app';
import useNotifications from '../../hooks/use-notifications';
import { mapNotificationsToFlashbar } from '../common/notification-flashbar';
import { useAppLayoutLabels } from '../common/labels';
import Navigation from '../genaiidp-layout/navigation';
import { inboxHref } from '../../routes/constants';
import InboxPage from './InboxPage';
import { InboxToolsPanel } from '../home/home-tools-panels';

const messages = defineMessages<{ home: NoMessageValues; inbox: NoMessageValues; ariaLabel: NoMessageValues }>({
  home: {
    id: 'breadcrumbs.inbox.home',
    defaultMessage: 'Home',
    description: 'First crumb of the breadcrumb trail on the Inbox page: the Home page.',
  },
  inbox: {
    id: 'breadcrumbs.inbox.inbox',
    defaultMessage: 'Inbox',
    description: 'Last crumb of the breadcrumb trail on the Inbox page: the Inbox itself.',
  },
  ariaLabel: {
    id: 'breadcrumbs.inbox.ariaLabel',
    defaultMessage: 'Breadcrumbs',
    description: 'Screen-reader name of the breadcrumb navigation landmark on the Inbox page.',
  },
});

const InboxLayout = (): React.JSX.Element => {
  const intl = useIntl();
  const { navigationOpen, setNavigationOpen } = useAppContext();
  const appLayoutLabels = useAppLayoutLabels();
  const flashItems = mapNotificationsToFlashbar(useNotifications());
  // The info panel, closed until asked for, as on the other Home pages.
  const [toolsOpen, setToolsOpen] = useState(false);
  const breadcrumbs = [
    { text: intl.formatMessage(messages.home), href: '#/' },
    { text: intl.formatMessage(messages.inbox), href: inboxHref() },
  ];

  return (
    <AppLayout
      headerSelector="#top-navigation"
      ariaLabels={appLayoutLabels}
      navigation={<Navigation />}
      navigationOpen={navigationOpen}
      onNavigationChange={({ detail }) => setNavigationOpen(detail.open)}
      notifications={<Flashbar items={flashItems} />}
      breadcrumbs={<BreadcrumbGroup ariaLabel={intl.formatMessage(messages.ariaLabel)} items={breadcrumbs} />}
      tools={<InboxToolsPanel />}
      toolsOpen={toolsOpen}
      onToolsChange={({ detail }) => setToolsOpen(detail.open)}
      content={<InboxPage />}
    />
  );
};

export default InboxLayout;
