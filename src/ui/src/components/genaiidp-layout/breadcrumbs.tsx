// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React from 'react';
import { Route, Routes, useLocation } from 'react-router-dom';
import { defineMessages, useIntl, type IntlShape, type NoMessageValues, type TypedMessageDescriptor } from 'react-intl';

import { BreadcrumbGroup } from '@cloudscape-design/components';

import DocumentListBreadCrumbs, { documentListBreadcrumbItems } from '../document-list/breadcrumbs';
import DocumentDetailsBreadCrumbs from '../document-details/breadcrumbs';
import ConfigurationBreadCrumbs from '../configuration-layout/breadcrumbs';
import UploadDocumentBreadCrumbs from '../upload-document/breadcrumbs';
import ConfigPrefixMappingsBreadCrumbs from '../config-prefix-mappings/breadcrumbs';
import {
  CAPACITY_PLANNING_PATH,
  CUSTOM_MODELS_PATH,
  DEFAULT_PATH,
  DISCOVERY_PATH,
  DOCUMENTS_ANALYTICS_PATH,
  DOCUMENTS_KB_QUERY_PATH,
  FEATURES_PATH_PREFIX,
  MODEL_CONFIG_LIMITS_PATH,
  PRICING_PATH,
  USER_MANAGEMENT_PATH,
} from '../../routes/constants';

const messages = defineMessages<{
  root: NoMessageValues;
  extensions: NoMessageValues;
  ariaLabel: NoMessageValues;
  knowledgeBase: NoMessageValues;
  documentAnalytics: NoMessageValues;
  pricing: NoMessageValues;
  modelLimits: NoMessageValues;
  capacityPlanning: NoMessageValues;
  customModels: NoMessageValues;
  discovery: NoMessageValues;
  userManagement: NoMessageValues;
}>({
  root: {
    id: 'breadcrumbs.layout.root',
    defaultMessage: 'Document Processing',
    description:
      'First crumb of the breadcrumb trail on the Extensions catalog pages: the application home, which opens the document list.',
  },
  extensions: {
    id: 'breadcrumbs.layout.extensions',
    defaultMessage: 'Extensions',
    description: 'Last crumb of the breadcrumb trail on the Extensions catalog and on an extension page.',
  },
  ariaLabel: {
    id: 'breadcrumbs.layout.ariaLabel',
    defaultMessage: 'Breadcrumbs',
    description:
      'Screen-reader name of the breadcrumb navigation landmark on the Extensions catalog and the configuration and analysis pages.',
  },
  knowledgeBase: {
    id: 'breadcrumbs.page.knowledgeBase',
    defaultMessage: 'Knowledge Base',
    description: 'Last crumb of the breadcrumb trail on the Knowledge Base query page, after Document Processing and Documents.',
  },
  documentAnalytics: {
    id: 'breadcrumbs.page.documentAnalytics',
    defaultMessage: 'Document Analytics',
    description: 'Last crumb of the breadcrumb trail on the Document Analytics page, after Document Processing and Documents.',
  },
  pricing: {
    id: 'breadcrumbs.page.pricing',
    defaultMessage: 'Pricing',
    description: 'Last crumb of the breadcrumb trail on the Pricing configuration page, after Document Processing and Documents.',
  },
  modelLimits: {
    id: 'breadcrumbs.page.modelLimits',
    defaultMessage: 'Model Limits',
    description: 'Last crumb of the breadcrumb trail on the Model Limits configuration page, after Document Processing and Documents.',
  },
  capacityPlanning: {
    id: 'breadcrumbs.page.capacityPlanning',
    defaultMessage: 'Capacity Planning',
    description: 'Last crumb of the breadcrumb trail on the Capacity Planning page, after Document Processing and Documents.',
  },
  customModels: {
    id: 'breadcrumbs.page.customModels',
    defaultMessage: 'Custom Models',
    description:
      'Last crumb of the breadcrumb trail on the Custom Models page and on a fine-tuning job, after Document Processing and Documents.',
  },
  discovery: {
    id: 'breadcrumbs.page.discovery',
    defaultMessage: 'Discovery',
    description:
      'Last crumb of the breadcrumb trail on the Discovery page and on a discovery job, after Document Processing and Documents.',
  },
  userManagement: {
    id: 'breadcrumbs.page.userManagement',
    defaultMessage: 'User Management',
    description: 'Last crumb of the breadcrumb trail on the User Management page, after Document Processing and Documents.',
  },
});

/**
 * Pages this layout hosts that are not documents, keyed by their absolute path.
 *
 * The crumbs below are matched relative to wherever the layout is mounted, and two
 * mounts (`/features/*` and `/documents/query/*`) leave nothing to match, so they fell
 * through to the Document List's crumbs; under `/documents/*` every other page's slug
 * reached the `:objectKey` route and was shown as though it were a document id
 * ("Documents > capacity-planning"). A page here is matched on the absolute path,
 * its own path or anything beneath it (a discovery job, a fine-tuning job).
 */
export const PAGE_BREADCRUMBS: ReadonlyArray<{ path: string; message: TypedMessageDescriptor<NoMessageValues> }> = [
  { path: DOCUMENTS_KB_QUERY_PATH, message: messages.knowledgeBase },
  { path: DOCUMENTS_ANALYTICS_PATH, message: messages.documentAnalytics },
  { path: PRICING_PATH, message: messages.pricing },
  { path: MODEL_CONFIG_LIMITS_PATH, message: messages.modelLimits },
  { path: CAPACITY_PLANNING_PATH, message: messages.capacityPlanning },
  { path: CUSTOM_MODELS_PATH, message: messages.customModels },
  { path: DISCOVERY_PATH, message: messages.discovery },
  { path: USER_MANAGEMENT_PATH, message: messages.userManagement },
];

export const pageBreadcrumbItems = (pathname: string, intl: IntlShape): { text: string; href: string }[] | null => {
  if (pathname === FEATURES_PATH_PREFIX || pathname.startsWith(`${FEATURES_PATH_PREFIX}/`)) {
    return [
      { text: intl.formatMessage(messages.root), href: `#${DEFAULT_PATH}` },
      { text: intl.formatMessage(messages.extensions), href: `#${FEATURES_PATH_PREFIX}` },
    ];
  }
  const page = PAGE_BREADCRUMBS.find(({ path }) => pathname === path || pathname.startsWith(`${path}/`));
  return page ? [...documentListBreadcrumbItems(intl), { text: intl.formatMessage(page.message), href: `#${page.path}` }] : null;
};

const Breadcrumbs = (): React.JSX.Element => {
  const { pathname } = useLocation();
  const intl = useIntl();
  const pageItems = pageBreadcrumbItems(pathname, intl);
  if (pageItems) {
    return <BreadcrumbGroup ariaLabel={intl.formatMessage(messages.ariaLabel)} items={pageItems} />;
  }
  return (
    <Routes>
      <Route index element={<DocumentListBreadCrumbs />} />
      <Route path="config" element={<ConfigurationBreadCrumbs />} />
      <Route path="upload" element={<UploadDocumentBreadCrumbs />} />
      <Route path="prefix-mappings" element={<ConfigPrefixMappingsBreadCrumbs />} />
      <Route path=":objectKey" element={<DocumentDetailsBreadCrumbs />} />
    </Routes>
  );
};

export default Breadcrumbs;
