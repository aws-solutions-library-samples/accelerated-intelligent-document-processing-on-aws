// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React from 'react';
import { useParams } from 'react-router-dom';

import { BreadcrumbGroup } from '@cloudscape-design/components';

import { DOCUMENTS_PATH } from '../../routes/constants';
import { documentListBreadcrumbItems } from '../document-list/breadcrumbs';

const Breadcrumbs = (): React.JSX.Element => {
  const { objectKey } = useParams();
  // The router has already decoded the key once.
  const decodedDocumentId = objectKey ?? '';
  // Encode the key once, as one segment, so slashes in it survive
  const encodedObjectKey = encodeURIComponent(decodedDocumentId);
  const documentDetailsBreadcrumbItems = [
    ...documentListBreadcrumbItems,
    { text: decodedDocumentId, href: `#${DOCUMENTS_PATH}/${encodedObjectKey}` },
  ];

  return <BreadcrumbGroup ariaLabel="Breadcrumbs" items={documentDetailsBreadcrumbItems} />;
};

export default Breadcrumbs;
