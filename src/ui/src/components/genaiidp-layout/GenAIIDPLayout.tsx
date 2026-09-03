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

import { DOCUMENT_LIST_SHARDS_PER_DAY, LATEST_PERIODS, PERIODS_TO_LOAD_STORAGE_KEY } from '../document-list/documents-table-config';

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
    // default to 2 hours - half of one (4hr) shard period
    let periods = 0.5;
    try {
      const stored = JSON.parse(localStorage.getItem(PERIODS_TO_LOAD_STORAGE_KEY) ?? '0');
      // Checked before the Math.abs below, which would otherwise turn the negative
      // Latest sentinel into a plausible-looking shard count (2 periods / 8 hours).
      if (stored === LATEST_PERIODS) {
        return LATEST_PERIODS;
      }
      const periodsFromStorage = Math.abs(stored);
      // prettier-ignore
      if (
        !Number.isFinite(periodsFromStorage)
        // load max of to 30 days
        || periodsFromStorage > DOCUMENT_LIST_SHARDS_PER_DAY * 30
      ) {
        logger.warn('invalid initialPeriodsToLoad value from local storage');
      } else {
        periods = (periodsFromStorage > 0) ? periodsFromStorage : periods;
        localStorage.setItem(PERIODS_TO_LOAD_STORAGE_KEY, JSON.stringify(periods));
      }
    } catch {
      logger.warn('failed to parse initialPeriodsToLoad from local storage');
    }

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
