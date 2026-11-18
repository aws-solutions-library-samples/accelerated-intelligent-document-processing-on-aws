// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React from 'react';
import { useParams } from 'react-router-dom';
import { defineMessages, useIntl, type NoMessageValues } from 'react-intl';

import { BreadcrumbGroup } from '@cloudscape-design/components';

import { DOCUMENTS_PATH } from '../../routes/constants';
import { documentListBreadcrumbItems } from '../document-list/breadcrumbs';

const messages = defineMessages<{ ariaLabel: NoMessageValues }>({
  ariaLabel: {
    id: 'breadcrumbs.documentDetails.ariaLabel',
    defaultMessage: 'Breadcrumbs',
    description: 'Screen-reader name of the breadcrumb navigation landmark on a document page.',
  },
});

const Breadcrumbs = (): React.JSX.Element => {
  const { objectKey } = useParams();
  const intl = useIntl();
  // The router has already decoded the key once.
  const decodedDocumentId = objectKey ?? '';
  // Encode the key once, as one segment, so slashes in it survive
  const encodedObjectKey = encodeURIComponent(decodedDocumentId);
  const documentDetailsBreadcrumbItems = [
    ...documentListBreadcrumbItems(intl),
    { text: decodedDocumentId, href: `#${DOCUMENTS_PATH}/${encodedObjectKey}` },
  ];

  return <BreadcrumbGroup ariaLabel={intl.formatMessage(messages.ariaLabel)} items={documentDetailsBreadcrumbItems} />;
};

export default Breadcrumbs;
