// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React from 'react';
import { defineMessages, useIntl, type IntlShape, type NoMessageValues, type TypedMessageDescriptor } from 'react-intl';

import { BreadcrumbGroup } from '@cloudscape-design/components';

import { DOCUMENTS_PATH, DEFAULT_PATH } from '../../routes/constants';

const messages = defineMessages<{ root: NoMessageValues; documents: NoMessageValues; ariaLabel: NoMessageValues }>({
  root: {
    id: 'breadcrumbs.documentList.root',
    defaultMessage: 'Document Processing',
    description:
      'First crumb of the breadcrumb trail on the document list and on every page under it: the application home, which opens the document list.',
  },
  documents: {
    id: 'breadcrumbs.documentList.documents',
    defaultMessage: 'Documents',
    description: 'Second crumb of the breadcrumb trail on the document list and on every page under it: the document list itself.',
  },
  ariaLabel: {
    id: 'breadcrumbs.documentList.ariaLabel',
    defaultMessage: 'Breadcrumbs',
    description: 'Screen-reader name of the breadcrumb navigation landmark on the document list page.',
  },
});

/** The label in the UI's language; without `intl` (the Review Studio trails until their own conversion) the English source text. */
const labelOf = (message: TypedMessageDescriptor<NoMessageValues>, intl?: IntlShape): string =>
  intl ? intl.formatMessage(message) : String(message.defaultMessage);

/** The two crumbs every document trail starts with, formatted through `intl`; without it, the English source text. */
export const documentListBreadcrumbItems = (intl?: IntlShape): { text: string; href: string }[] => [
  { text: labelOf(messages.root, intl), href: `#${DEFAULT_PATH}` },
  { text: labelOf(messages.documents, intl), href: `#${DOCUMENTS_PATH}` },
];

const Breadcrumbs = (): React.JSX.Element => {
  const intl = useIntl();
  return <BreadcrumbGroup ariaLabel={intl.formatMessage(messages.ariaLabel)} items={documentListBreadcrumbItems(intl)} />;
};

export default Breadcrumbs;
