// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// A document page's title: "<document> (<status>)" over the object key, with the page's
// action bar. The edit page and the document page share it.
import React from 'react';
import { Box, Header } from '@cloudscape-design/components';

interface DocumentPageHeaderProps {
  /** The document's name. */
  title: string;
  /** Shown after the title in parentheses, as text: the item's or the document's status. */
  status?: string | null;
  /** The full object key, shown under the title. */
  objectKey: string;
  /** The page's action bar. */
  actions?: React.ReactNode;
}

const DocumentPageHeader = ({ title, status, objectKey, actions }: DocumentPageHeaderProps): React.JSX.Element => (
  <Header variant="h1" description={<span title={objectKey}>{objectKey}</span>} actions={actions}>
    {title}
    {status && (
      <>
        {' '}
        {/* The status reads as the subtitle does (font, size and colour), not as the title. */}
        <Box variant="span" fontSize="body-m" fontWeight="normal" color="text-body-secondary">
          ({status})
        </Box>
      </>
    )}
  </Header>
);

export default DocumentPageHeader;
