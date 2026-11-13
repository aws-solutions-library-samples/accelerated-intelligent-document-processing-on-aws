// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React from 'react';
import { Route, Routes } from 'react-router-dom';

import DocumentListToolsPanel from '../document-list/tools-panel';
import DocumentDetailsToolsPanel from '../document-details/tools-panel';
import ConfigurationToolsPanel from '../configuration-layout/tools-panel';
import UploadDocumentToolsPanel from '../upload-document/tools-panel';
import DocumentsQueryToolsPanel from '../document-kb-query-layout/tools-panel';
import PricingToolsPanel from '../pricing-layout/tools-panel';
import CapacityPlanningToolsPanel from '../capacity-planning/tools-panel';
import CustomModelsToolsPanel from '../custom-models/tools-panel';
import CustomModelsJobToolsPanel from '../custom-models/job-tools-panel';
import DiscoveryToolsPanel from '../discovery/tools-panel';
import DiscoveryJobToolsPanel from '../discovery/job-tools-panel';
import UserManagementToolsPanel from '../user-management/tools-panel';
import ConfigPrefixMappingsToolsPanel from '../config-prefix-mappings/tools-panel';

const ToolsPanel = (): React.JSX.Element => {
  return (
    <Routes>
      <Route index element={<DocumentListToolsPanel />} />
      <Route path="config" element={<ConfigurationToolsPanel />} />
      <Route path="upload" element={<UploadDocumentToolsPanel />} />
      <Route path="query" element={<DocumentsQueryToolsPanel />} />
      <Route path="pricing" element={<PricingToolsPanel />} />
      <Route path="capacity-planning" element={<CapacityPlanningToolsPanel />} />
      <Route path="custom-models" element={<CustomModelsToolsPanel />} />
      <Route path="custom-models/:jobId" element={<CustomModelsJobToolsPanel />} />
      <Route path="discovery" element={<DiscoveryToolsPanel />} />
      <Route path="discovery/job/:jobId" element={<DiscoveryJobToolsPanel />} />
      <Route path="users" element={<UserManagementToolsPanel />} />
      {/* Above the ":objectKey" catch-all below, or this page shows the document panel. */}
      <Route path="prefix-mappings" element={<ConfigPrefixMappingsToolsPanel />} />
      <Route path=":objectKey" element={<DocumentDetailsToolsPanel />} />
    </Routes>
  );
};

export default ToolsPanel;
