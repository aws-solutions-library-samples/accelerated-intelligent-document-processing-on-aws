// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The review workspace body: an item rail beside the work area, which holds the
 * section editor's page and field panes. Shared by Test Studio annotation and live
 * review queues; each adapter fills the slots with its own panels.
 *
 * - `header`: above the rail and work area (title, links, alerts).
 * - `context`: above the rail and work area, below the header (test-set progress, or a
 *   queue item's task context).
 * - `footer`: below both.
 *
 * Without a `rail` the work area takes the full width (the annotation screen, which
 * shows one item).
 */

import React from 'react';
import { Grid, SpaceBetween } from '@cloudscape-design/components';

interface ReviewWorkspaceLayoutProps {
  rail?: React.ReactNode;
  /** The work area: the selected item's actions, notices and section editor. */
  children: React.ReactNode;
  railCollapsed?: boolean;
  header?: React.ReactNode;
  context?: React.ReactNode;
  footer?: React.ReactNode;
}

const EXPANDED = [{ colspan: { default: 12, m: 3 } }, { colspan: { default: 12, m: 9 } }];
const COLLAPSED = [{ colspan: { default: 12, m: 1 } }, { colspan: { default: 12, m: 11 } }];

const ReviewWorkspaceLayout = ({
  rail,
  children,
  railCollapsed = false,
  header,
  context,
  footer,
}: ReviewWorkspaceLayoutProps): React.JSX.Element => (
  <SpaceBetween size="l">
    {header}
    {context}
    {rail ? (
      <Grid gridDefinition={railCollapsed ? COLLAPSED : EXPANDED}>
        {rail}
        <SpaceBetween size="s">{children}</SpaceBetween>
      </Grid>
    ) : (
      <SpaceBetween size="s">{children}</SpaceBetween>
    )}
    {footer}
  </SpaceBetween>
);

export default ReviewWorkspaceLayout;
