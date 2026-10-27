// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The HITL pages' AppLayout (the InboxLayout shape): shared navigation and Flashbar,
// breadcrumbs Home › Queue Management › page (or Home › Home › page).
import React, { useState } from 'react';
import { AppLayout, BreadcrumbGroup, Flashbar } from '@cloudscape-design/components';
import type { BreadcrumbGroupProps } from '@cloudscape-design/components';

import useAppContext from '../../contexts/app';
import useNotifications from '../../hooks/use-notifications';
import { mapNotificationsToFlashbar } from '../common/notification-flashbar';
import { appLayoutLabels } from '../common/labels';
import Navigation from '../genaiidp-layout/navigation';
import { REVIEW_QUEUES_PATH } from '../../routes/constants';

/** HITL › Queue Management. */
const QUEUE_MANAGEMENT_CRUMB: BreadcrumbGroupProps.Item = { text: 'Queue Management', href: `#${REVIEW_QUEUES_PATH}` };

interface ReviewQueuesLayoutProps {
  /** Breadcrumbs after "Home › Queue Management" (or after `root`). */
  crumbs?: BreadcrumbGroupProps.Item[];
  /** The section crumb; Queue Management (HITL) by default. */
  root?: BreadcrumbGroupProps.Item;
  children: React.ReactNode;
  contentType?: 'default' | 'table' | 'wizard';
  /** The info panel the info icon opens, as on the document pages; omitted, there is none. */
  tools?: React.ReactNode;
}

const ReviewQueuesLayout = ({
  crumbs = [],
  root,
  children,
  contentType = 'default',
  tools,
}: ReviewQueuesLayoutProps): React.JSX.Element => {
  const { navigationOpen, setNavigationOpen } = useAppContext();
  const flashItems = mapNotificationsToFlashbar(useNotifications());
  // Closed until asked for, as on the document pages.
  const [toolsOpen, setToolsOpen] = useState(false);
  const items = [{ text: 'Home', href: '#/' }, root ?? QUEUE_MANAGEMENT_CRUMB, ...crumbs];

  return (
    <AppLayout
      headerSelector="#top-navigation"
      ariaLabels={appLayoutLabels}
      navigation={<Navigation />}
      navigationOpen={navigationOpen}
      onNavigationChange={({ detail }) => setNavigationOpen(detail.open)}
      notifications={<Flashbar items={flashItems} />}
      breadcrumbs={<BreadcrumbGroup ariaLabel="Breadcrumbs" items={items} />}
      contentType={contentType}
      tools={tools}
      toolsHide={!tools}
      toolsOpen={toolsOpen}
      onToolsChange={({ detail }) => setToolsOpen(detail.open)}
      content={children}
    />
  );
};

export default ReviewQueuesLayout;
