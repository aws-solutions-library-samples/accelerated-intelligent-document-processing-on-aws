// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React from 'react';
import { Route, Routes } from 'react-router-dom';

import DocumentListBreadCrumbs from '../document-list/breadcrumbs';
import DocumentDetailsBreadCrumbs from '../document-details/breadcrumbs';
import ConfigurationBreadCrumbs from '../configuration-layout/breadcrumbs';
import UploadDocumentBreadCrumbs from '../upload-document/breadcrumbs';
import ConfigPrefixMappingsBreadCrumbs from '../config-prefix-mappings/breadcrumbs';

const Breadcrumbs = (): React.JSX.Element => {
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
