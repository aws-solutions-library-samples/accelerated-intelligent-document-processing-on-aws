// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React from 'react';
import { Route, Routes, useLocation } from 'react-router-dom';

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
export const PAGE_BREADCRUMBS: ReadonlyArray<{ path: string; text: string }> = [
  { path: DOCUMENTS_KB_QUERY_PATH, text: 'Knowledge Base' },
  { path: DOCUMENTS_ANALYTICS_PATH, text: 'Document Analytics' },
  { path: PRICING_PATH, text: 'Pricing' },
  { path: MODEL_CONFIG_LIMITS_PATH, text: 'Model Limits' },
  { path: CAPACITY_PLANNING_PATH, text: 'Capacity Planning' },
  { path: CUSTOM_MODELS_PATH, text: 'Custom Models' },
  { path: DISCOVERY_PATH, text: 'Discovery' },
  { path: USER_MANAGEMENT_PATH, text: 'User Management' },
];

export const pageBreadcrumbItems = (pathname: string): { text: string; href: string }[] | null => {
  if (pathname === FEATURES_PATH_PREFIX || pathname.startsWith(`${FEATURES_PATH_PREFIX}/`)) {
    return [
      { text: 'Document Processing', href: `#${DEFAULT_PATH}` },
      { text: 'Extensions', href: `#${FEATURES_PATH_PREFIX}` },
    ];
  }
  const page = PAGE_BREADCRUMBS.find(({ path }) => pathname === path || pathname.startsWith(`${path}/`));
  return page ? [...documentListBreadcrumbItems, { text: page.text, href: `#${page.path}` }] : null;
};

const Breadcrumbs = (): React.JSX.Element => {
  const { pathname } = useLocation();
  const pageItems = pageBreadcrumbItems(pathname);
  if (pageItems) {
    return <BreadcrumbGroup ariaLabel="Breadcrumbs" items={pageItems} />;
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
