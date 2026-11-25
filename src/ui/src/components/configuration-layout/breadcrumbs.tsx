// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

// src/components/configuration-layout/breadcrumbs.jsx
import React from 'react';
import { defineMessages, useIntl, type NoMessageValues } from 'react-intl';
import { BreadcrumbGroup } from '@cloudscape-design/components';
import { DOCUMENTS_PATH, CONFIGURATION_PATH, DEFAULT_PATH } from '../../routes/constants';

const messages = defineMessages<{
  root: NoMessageValues;
  documents: NoMessageValues;
  configuration: NoMessageValues;
  ariaLabel: NoMessageValues;
}>({
  root: {
    id: 'breadcrumbs.configuration.root',
    defaultMessage: 'Document Processing',
    description: 'First crumb of the breadcrumb trail on the Configuration page: the application home, which opens the document list.',
  },
  documents: {
    id: 'breadcrumbs.configuration.documents',
    defaultMessage: 'Documents',
    description: 'Second crumb of the breadcrumb trail on the Configuration page: the document list.',
  },
  configuration: {
    id: 'breadcrumbs.configuration.configuration',
    defaultMessage: 'Configuration',
    description: 'Last crumb of the breadcrumb trail on the Configuration page: the page itself.',
  },
  ariaLabel: {
    id: 'breadcrumbs.configuration.ariaLabel',
    defaultMessage: 'Breadcrumbs',
    description: 'Screen-reader name of the breadcrumb navigation landmark on the Configuration page.',
  },
});

const Breadcrumbs = (): React.JSX.Element => {
  const intl = useIntl();
  const configurationBreadcrumbItems = [
    { text: intl.formatMessage(messages.root), href: `#${DEFAULT_PATH}` },
    { text: intl.formatMessage(messages.documents), href: `#${DOCUMENTS_PATH}` },
    { text: intl.formatMessage(messages.configuration), href: `#${CONFIGURATION_PATH}` },
  ];
  return <BreadcrumbGroup ariaLabel={intl.formatMessage(messages.ariaLabel)} items={configurationBreadcrumbItems} />;
};

export default Breadcrumbs;
