// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Its own AppLayout rather than GenAIIDPLayout, which mounts the document list and
// its poll. The Flashbar is the app's shared one, so action results show here too.
import React from 'react';
import { AppLayout, BreadcrumbGroup, Flashbar } from '@cloudscape-design/components';

import useAppContext from '../../contexts/app';
import useNotifications from '../../hooks/use-notifications';
import { mapNotificationsToFlashbar } from '../common/notification-flashbar';
import { appLayoutLabels } from '../common/labels';
import Navigation from '../genaiidp-layout/navigation';
import { inboxHref } from '../../routes/constants';
import InboxPage from './InboxPage';

const BREADCRUMBS = [
  { text: 'Home', href: '#/' },
  { text: 'Inbox', href: inboxHref() },
];

const InboxLayout = (): React.JSX.Element => {
  const { navigationOpen, setNavigationOpen } = useAppContext();
  const flashItems = mapNotificationsToFlashbar(useNotifications());

  return (
    <AppLayout
      headerSelector="#top-navigation"
      ariaLabels={appLayoutLabels}
      navigation={<Navigation />}
      navigationOpen={navigationOpen}
      onNavigationChange={({ detail }) => setNavigationOpen(detail.open)}
      notifications={<Flashbar items={flashItems} />}
      breadcrumbs={<BreadcrumbGroup ariaLabel="Breadcrumbs" items={BREADCRUMBS} />}
      toolsHide
      content={<InboxPage />}
    />
  );
};

export default InboxLayout;
