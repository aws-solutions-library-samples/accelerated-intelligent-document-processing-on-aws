// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React from 'react';
import { defineMessages, useIntl, type NoMessageValues } from 'react-intl';
import { BreadcrumbGroup } from '@cloudscape-design/components';
import { DOCUMENTS_PATH, CONFIG_PREFIX_MAPPINGS_PATH, DEFAULT_PATH } from '../../routes/constants';

const messages = defineMessages<{
  root: NoMessageValues;
  documents: NoMessageValues;
  prefixMappings: NoMessageValues;
  ariaLabel: NoMessageValues;
}>({
  root: {
    id: 'breadcrumbs.prefixMappings.root',
    defaultMessage: 'Document Processing',
    description: 'First crumb of the breadcrumb trail on the Prefix Mappings page: the application home, which opens the document list.',
  },
  documents: {
    id: 'breadcrumbs.prefixMappings.documents',
    defaultMessage: 'Documents',
    description: 'Second crumb of the breadcrumb trail on the Prefix Mappings page: the document list.',
  },
  prefixMappings: {
    id: 'breadcrumbs.prefixMappings.prefixMappings',
    defaultMessage: 'Prefix Mappings',
    description: 'Last crumb of the breadcrumb trail on the Prefix Mappings page: the page itself.',
  },
  ariaLabel: {
    id: 'breadcrumbs.prefixMappings.ariaLabel',
    defaultMessage: 'Breadcrumbs',
    description: 'Screen-reader name of the breadcrumb navigation landmark on the Prefix Mappings page.',
  },
});

const Breadcrumbs = (): React.JSX.Element => {
  const intl = useIntl();
  const configPrefixMappingsBreadcrumbItems = [
    { text: intl.formatMessage(messages.root), href: `#${DEFAULT_PATH}` },
    { text: intl.formatMessage(messages.documents), href: `#${DOCUMENTS_PATH}` },
    { text: intl.formatMessage(messages.prefixMappings), href: `#${CONFIG_PREFIX_MAPPINGS_PATH}` },
  ];
  return <BreadcrumbGroup ariaLabel={intl.formatMessage(messages.ariaLabel)} items={configPrefixMappingsBreadcrumbItems} />;
};

export default Breadcrumbs;
