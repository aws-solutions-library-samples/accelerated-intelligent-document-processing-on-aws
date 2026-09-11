// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React, { useState } from 'react';
import { Routes, Route } from 'react-router-dom';
import { AppLayout, Flashbar } from '@cloudscape-design/components';

import { ConsoleLogger } from 'aws-amplify/utils';

import { DocumentsContext } from '../../contexts/documents';
import { Document } from '../../types/documents';

import useNotifications from '../../hooks/use-notifications';
import { mapNotificationsToFlashbar } from '../common/notification-flashbar';
import useGraphQlApi from '../../hooks/use-graphql-api';
import useAppContext from '../../contexts/app';

import DocumentList from '../document-list';
import DocumentDetails from '../document-details';
import DocumentsQueryLayout from '../document-kb-query-layout';
import DocumentsAgentsLayout from '../document-agents-layout/DocumentsAgentsLayout';
import UploadDocumentPanel from '../upload-document';
import DiscoveryPage from '../discovery/DiscoveryPage';
import DiscoveryJobDetails from '../discovery/DiscoveryJobDetails';
import UserManagementLayout from '../user-management/UserManagementLayout';
import { appLayoutLabels } from '../common/labels';

import Navigation from './navigation';
import Breadcrumbs from './breadcrumbs';
import ToolsPanel from './tools-panel';
import ConfigurationLayout from '../configuration-layout';
import PricingLayout from '../pricing-layout';
import ModelConfigLimitsLayout from '../model-config-limits-layout';
import CapacityPlanningLayout from '../capacity-planning/CapacityPlanningLayout';
import CustomModelsLayout from '../custom-models/CustomModelsLayout';
import { FinetuningJobDetail } from '../custom-models';

import { LATEST_PERIODS, PERIODS_TO_LOAD_STORAGE_KEY, resolveInitialPeriodsToLoad } from '../document-list/documents-table-config';

const logger = new ConsoleLogger('GenAIIDPLayout');

interface GenAIIDPLayoutProps {
  children?: React.ReactNode;
  /** Override the right-side info (Tools) panel. Defaults to the document ToolsPanel. */
  tools?: React.ReactNode;
}

const GenAIIDPLayout = ({ children, tools }: GenAIIDPLayoutProps): React.JSX.Element => {
  const { navigationOpen, setNavigationOpen } = useAppContext();

  const notifications = useNotifications();
  const [toolsOpen, setToolsOpen] = useState(false);
  const [selectedItems, setSelectedItems] = useState<Document[]>([]);

  const getInitialPeriodsToLoad = () => {
    let periods = LATEST_PERIODS;
    try {
      periods = resolveInitialPeriodsToLoad(JSON.parse(localStorage.getItem(PERIODS_TO_LOAD_STORAGE_KEY) ?? '0'));
    } catch {
      logger.warn('failed to parse initialPeriodsToLoad from local storage');
    }
    // Written back so the resolved scope is what the next visit reads, including
    // when an out-of-range or unparseable value was replaced by the default.
    localStorage.setItem(PERIODS_TO_LOAD_STORAGE_KEY, JSON.stringify(periods));
    return periods;
  };
  const initialPeriodsToLoad = getInitialPeriodsToLoad();

  const {
    documents,
    getDocumentDetailsFromIds,
    isDocumentsListLoading,
    hasListBeenLoaded,
    periodsToLoad,
    setIsDocumentsListLoading,
    setPeriodsToLoad,
    customDateRange,
    setCustomDateRange,
    documentView,
    setDocumentView,
    latestTruncated,
    deleteDocuments,
    reprocessDocuments,
    abortWorkflows,
  } = useGraphQlApi({ initialPeriodsToLoad });

  const documentsContextValue = {
    documents,
    getDocumentDetailsFromIds,
    isDocumentsListLoading,
    hasListBeenLoaded,
    selectedItems,
    setIsDocumentsListLoading,
    setPeriodsToLoad,
    setToolsOpen,
    setSelectedItems,
    periodsToLoad,
    customDateRange,
    setCustomDateRange,
    documentView,
    setDocumentView,
    latestTruncated,
    toolsOpen,
    deleteDocuments,
    reprocessDocuments,
    abortWorkflows,
  };

  return (
    <DocumentsContext.Provider value={documentsContextValue}>
      <AppLayout
        headerSelector="#top-navigation"
        navigation={<Navigation />}
        navigationOpen={navigationOpen}
        onNavigationChange={({ detail }) => setNavigationOpen(detail.open)}
        breadcrumbs={<Breadcrumbs />}
        notifications={<Flashbar items={mapNotificationsToFlashbar(notifications)} />}
        tools={tools ?? <ToolsPanel />}
        toolsOpen={toolsOpen}
        onToolsChange={({ detail }) => setToolsOpen(detail.open)}
        content={
          children || (
            <Routes>
              <Route index element={<DocumentList />} />
              <Route path="query" element={<DocumentsQueryLayout />} />
              <Route path="agents" element={<DocumentsAgentsLayout />} />
              <Route path="config" element={<ConfigurationLayout />} />
              <Route path="pricing" element={<PricingLayout />} />
              <Route path="model-limits" element={<ModelConfigLimitsLayout />} />
              <Route path="capacity-planning" element={<CapacityPlanningLayout />} />
              <Route path="custom-models" element={<CustomModelsLayout />} />
              <Route path="custom-models/:jobId" element={<FinetuningJobDetail />} />
              <Route path="upload" element={<UploadDocumentPanel />} />
              <Route path="discovery" element={<DiscoveryPage />} />
              <Route path="discovery/job/:jobId" element={<DiscoveryJobDetails />} />
              <Route path="users" element={<UserManagementLayout />} />
              <Route path="*" element={<DocumentDetails />} />
            </Routes>
          )
        }
        ariaLabels={appLayoutLabels}
      />
    </DocumentsContext.Provider>
  );
};

export default GenAIIDPLayout;
