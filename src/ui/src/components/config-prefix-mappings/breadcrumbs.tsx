// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React from 'react';
import { BreadcrumbGroup } from '@cloudscape-design/components';
import { DOCUMENTS_PATH, CONFIG_PREFIX_MAPPINGS_PATH, DEFAULT_PATH } from '../../routes/constants';

export const configPrefixMappingsBreadcrumbItems = [
  { text: 'Document Processing', href: `#${DEFAULT_PATH}` },
  { text: 'Documents', href: `#${DOCUMENTS_PATH}` },
  { text: 'Prefix Mappings', href: `#${CONFIG_PREFIX_MAPPINGS_PATH}` },
];

const Breadcrumbs = (): React.JSX.Element => <BreadcrumbGroup ariaLabel="Breadcrumbs" items={configPrefixMappingsBreadcrumbItems} />;

export default Breadcrumbs;
