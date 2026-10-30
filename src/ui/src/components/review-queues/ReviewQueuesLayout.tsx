// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The Review Studio pages' AppLayout (the InboxLayout shape): shared navigation and
// Flashbar, and the page's breadcrumbs (see breadcrumbs.ts).
import React, { useState } from 'react';
import { AppLayout, BreadcrumbGroup, Flashbar } from '@cloudscape-design/components';
import type { BreadcrumbGroupProps } from '@cloudscape-design/components';

import useAppContext from '../../contexts/app';
import useNotifications from '../../hooks/use-notifications';
import { mapNotificationsToFlashbar } from '../common/notification-flashbar';
import { appLayoutLabels } from '../common/labels';
import Navigation from '../genaiidp-layout/navigation';
import { reviewManagementTrail } from './breadcrumbs';

interface ReviewQueuesLayoutProps {
  /** The whole trail; Review Studio › Review Management by default. */
  trail?: BreadcrumbGroupProps.Item[];
  children: React.ReactNode;
  contentType?: 'default' | 'table' | 'wizard';
  /** The info panel the info icon opens, as on the document pages; omitted, there is none. */
  tools?: React.ReactNode;
  /** The side-navigation entry to highlight; null for none; omitted, derived from the URL. */
  navActiveHref?: string | null;
}

const ReviewQueuesLayout = ({
  trail,
  children,
  contentType = 'default',
  tools,
  navActiveHref,
}: ReviewQueuesLayoutProps): React.JSX.Element => {
  const { navigationOpen, setNavigationOpen } = useAppContext();
  const flashItems = mapNotificationsToFlashbar(useNotifications());
  // Closed until asked for, as on the document pages.
  const [toolsOpen, setToolsOpen] = useState(false);
  const items = trail ?? reviewManagementTrail();

  return (
    <AppLayout
      headerSelector="#top-navigation"
      ariaLabels={appLayoutLabels}
      navigation={<Navigation activeHref={navActiveHref} />}
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
