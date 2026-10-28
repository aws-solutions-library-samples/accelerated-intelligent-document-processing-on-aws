// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The edit screen's body, top to bottom: the header (title, actions, notices), the
 * task context (a queue item's Review Reason) and the work area (the Edit Document
 * widget and the dialogs its actions open). Every mode of the edit screen uses it
 * through EditScreen, so the order is the same whichever route opened the document.
 */

import React from 'react';
import { SpaceBetween } from '@cloudscape-design/components';

interface EditScreenLayoutProps {
  header: React.ReactNode;
  context?: React.ReactNode;
  /** The work area: the Edit Document widget and the dialogs. */
  children: React.ReactNode;
}

const EditScreenLayout = ({ header, context, children }: EditScreenLayoutProps): React.JSX.Element => (
  <SpaceBetween size="l">
    {header}
    {context}
    <SpaceBetween size="s">{children}</SpaceBetween>
  </SpaceBetween>
);

export default EditScreenLayout;
