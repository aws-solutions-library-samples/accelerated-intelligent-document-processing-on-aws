// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React from 'react';
import { defineMessages, useIntl, type NoMessageValues } from 'react-intl';
import { BreadcrumbGroup } from '@cloudscape-design/components';
import { DOCUMENTS_PATH, UPLOAD_DOCUMENT_PATH, DEFAULT_PATH } from '../../routes/constants';

const messages = defineMessages<{
  root: NoMessageValues;
  documents: NoMessageValues;
  upload: NoMessageValues;
  ariaLabel: NoMessageValues;
}>({
  root: {
    id: 'breadcrumbs.uploadDocument.root',
    defaultMessage: 'Document Processing',
    description: 'First crumb of the breadcrumb trail on the Upload Documents page: the application home, which opens the document list.',
  },
  documents: {
    id: 'breadcrumbs.uploadDocument.documents',
    defaultMessage: 'Documents',
    description: 'Second crumb of the breadcrumb trail on the Upload Documents page: the document list.',
  },
  upload: {
    id: 'breadcrumbs.uploadDocument.upload',
    defaultMessage: 'Upload Documents',
    description: 'Last crumb of the breadcrumb trail on the Upload Documents page: the page itself.',
  },
  ariaLabel: {
    id: 'breadcrumbs.uploadDocument.ariaLabel',
    defaultMessage: 'Breadcrumbs',
    description: 'Screen-reader name of the breadcrumb navigation landmark on the Upload Documents page.',
  },
});

const Breadcrumbs = (): React.JSX.Element => {
  const intl = useIntl();
  const uploadDocumentBreadcrumbItems = [
    { text: intl.formatMessage(messages.root), href: `#${DEFAULT_PATH}` },
    { text: intl.formatMessage(messages.documents), href: `#${DOCUMENTS_PATH}` },
    { text: intl.formatMessage(messages.upload), href: `#${UPLOAD_DOCUMENT_PATH}` },
  ];
  return <BreadcrumbGroup ariaLabel={intl.formatMessage(messages.ariaLabel)} items={uploadDocumentBreadcrumbItems} />;
};

export default Breadcrumbs;
