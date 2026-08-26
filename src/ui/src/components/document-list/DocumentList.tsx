// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React, { useEffect, useState, useMemo } from 'react';
import { Table, Pagination, TextFilter, Box, SpaceBetween } from '@cloudscape-design/components';
import { useCollection } from '@cloudscape-design/collection-hooks';
import { ConsoleLogger } from 'aws-amplify/utils';
import { generateClient } from '../../api/client-shim';
import { fetchSharedAuthSession } from '../../api/auth-session';
import { useNavigate } from 'react-router-dom';

interface DateRange {
  startDateTime: string;
  endDateTime: string;
}

import useDocumentsContext from '../../contexts/documents';
import { Document } from '../../types/documents';
import useSettingsContext from '../../contexts/settings';
import useUserRole from '../../hooks/use-user-role';

import mapDocumentsAttributes from '../common/map-document-attributes';
import { paginationLabels } from '../common/labels';
import useLocalStorage from '../common/local-storage';
import { exportToExcel } from '../common/download-func';
import DeleteDocumentModal from '../common/DeleteDocumentModal';
import ReprocessDocumentModal from '../common/ReprocessDocumentModal';
import AbortWorkflowModal from '../common/AbortWorkflowModal';
import DateRangeModal from '../common/DateRangeModal';
import { claimReview, releaseReview } from '../../graphql/generated';

import type { MappedDocument } from './documents-table-config';
import {
  DocumentsPreferences,
  DocumentsCommonHeader,
  COLUMN_DEFINITIONS_MAIN,
  KEY_COLUMN_ID,
  UNIQUE_TRACK_ID,
  SELECTION_LABELS,
  DEFAULT_PREFERENCES,
  DEFAULT_SORT_COLUMN,
} from './documents-table-config';

import { getFilterCounterText, TableEmptyState, TableNoMatchState } from '../common/table';
import useConfigurationVersions from '../../hooks/use-configuration-versions';
import { formatConfigVersionText } from '../test-studio/utils/configVersionUtils';

import '@cloudscape-design/global-styles/index.css';

const logger = new ConsoleLogger('DocumentList');

const DocumentList = (): React.JSX.Element => {
  const { versions } = useConfigurationVersions();
  const [documentList, setDocumentList] = useState<MappedDocument[]>([]);
  const [isDeleteModalVisible, setIsDeleteModalVisible] = useState(false);
  const [isReprocessModalVisible, setIsReprocessModalVisible] = useState(false);
  const [isAbortModalVisible, setIsAbortModalVisible] = useState(false);
  const [isDeleteLoading, setIsDeleteLoading] = useState(false);
  const [isReprocessLoading, setIsReprocessLoading] = useState(false);
  const [isAbortLoading, setIsAbortLoading] = useState(false);
  const [isDateRangeModalVisible, setIsDateRangeModalVisible] = useState(false);
  const [currentUsername, setCurrentUsername] = useState('');
  const { settings: _settings } = useSettingsContext();
  const { isAdmin, isReviewerOnly, canWrite, canReview } = useUserRole();
  const navigate = useNavigate();

  // Get current username on mount
  useEffect(() => {
    const getUsername = async () => {
      try {
        const session = await fetchSharedAuthSession();
        setCurrentUsername((session?.tokens?.idToken?.payload?.['cognito:username'] as string) || '');
      } catch (e) {
        logger.error('Error getting username', e);
      }
    };
    getUsername();
  }, []);

  const {
    documents,
    isDocumentsListLoading,
    setIsDocumentsListLoading,
    setPeriodsToLoad,
    setSelectedItems,
    setToolsOpen,
    periodsToLoad,
    customDateRange,
    setCustomDateRange,
    getDocumentDetailsFromIds,
    deleteDocuments,
    reprocessDocuments,
    abortWorkflows,
    hasListBeenLoaded,
  } = useDocumentsContext();

  const [preferences, setPreferences] = useLocalStorage('documents-list-preferences', DEFAULT_PREFERENCES);

  // Trigger document list load when DocumentList mounts for the first time.
  // The useGraphQlApi hook no longer auto-triggers listDocuments on mount (to avoid
  // unnecessary API calls on non-list pages like document details or config).
  // If the list has already been loaded (e.g., navigating back from document details),
  // display the cached documents immediately — no re-fetch needed.
  // The user can always click Refresh to force a fresh fetch.
  useEffect(() => {
    if (!isDocumentsListLoading && !hasListBeenLoaded) {
      setIsDocumentsListLoading(true);
    }
  }, []);

  // Filter documents for reviewers - show only pending HITL reviews (not completed/skipped)
  // Note: Server-side RBAC filtering is now applied in the listDocuments resolver.
  // Reviewer-only users receive only HITL-pending + their own completed reviews from the API.
  // This client-side filter is kept as a secondary safety net but is no longer the primary enforcement.
  const filteredDocumentList = useMemo(() => {
    if (isReviewerOnly) {
      return documentList.filter((doc) => {
        // Must have HITL triggered
        if (!doc.hitlTriggered) return false;
        // Exclude completed or skipped reviews
        if (doc.hitlCompleted) return false;
        const status = doc.hitlStatus?.toLowerCase().replace(/\s+/g, '') || '';
        if (status === 'skipped' || status === 'reviewskipped') return false;
        if (status === 'completed' || status === 'reviewcompleted') return false;
        // Show if unassigned or assigned to current user
        return !doc.hitlReviewOwner || doc.hitlReviewOwner === currentUsername;
      });
    }
    return documentList;
  }, [documentList, isReviewerOnly, currentUsername]);

  // Custom empty state for reviewers
  const emptyState = useMemo(() => {
    if (isReviewerOnly) {
      return (
        <Box margin={{ vertical: 'xs' }} textAlign="center" color="inherit">
          <SpaceBetween size="xxs">
            <div>
              <b>No pending reviews</b>
              <Box variant="p" color="inherit">
                There are no documents waiting for your review at this time.
              </Box>
            </div>
          </SpaceBetween>
        </Box>
      );
    }
    return <TableEmptyState resourceName="Document" />;
  }, [isReviewerOnly]);

  // prettier-ignore
  const {
    items, actions, filteredItemsCount, collectionProps, filterProps, paginationProps,
  } = useCollection(filteredDocumentList, {
    filtering: {
      empty: emptyState,
      noMatch: <TableNoMatchState onClearFilter={() => actions.setFiltering('')} />,
    },
    pagination: { pageSize: preferences.pageSize },
    sorting: { defaultState: { sortingColumn: DEFAULT_SORT_COLUMN, isDescending: true } },
    selection: {
      keepSelection: false,
      trackBy: UNIQUE_TRACK_ID,
    },
  });

  useEffect(() => {
    if (!isDocumentsListLoading) {
      logger.debug('setting documents list', documents);
      setDocumentList(mapDocumentsAttributes(documents as unknown as { ObjectKey: string }[]) as MappedDocument[]);
    } else {
      logger.debug('documents list is loading');
    }
  }, [isDocumentsListLoading, documents]);

  useEffect(() => {
    logger.debug('setting selected items', collectionProps.selectedItems);
    setSelectedItems([...(collectionProps.selectedItems ?? [])] as unknown as Document[]);
  }, [collectionProps.selectedItems]);

  const handleDeleteConfirm = async () => {
    const objectKeys = (collectionProps.selectedItems as MappedDocument[]).map((item) => item.objectKey);
    logger.debug('Deleting documents', objectKeys);

    setIsDeleteLoading(true);
    try {
      const result = await deleteDocuments(objectKeys);
      logger.debug('Delete result', result);

      // Close the modal
      setIsDeleteModalVisible(false);

      // Clear selection after deletion
      actions.setSelectedItems([]);
    } finally {
      setIsDeleteLoading(false);
    }
  };

  const handleReprocessConfirm = async (version?: string) => {
    const objectKeys = (collectionProps.selectedItems as MappedDocument[]).map((item) => item.objectKey);
    logger.debug('Reprocessing documents', objectKeys, 'with version', version);

    setIsReprocessLoading(true);
    try {
      const result = await reprocessDocuments(objectKeys, version);
      logger.debug('Reprocess result', result);

      // Close the modal
      setIsReprocessModalVisible(false);

      // Clear selection after reprocessing
      actions.setSelectedItems([]);
    } finally {
      setIsReprocessLoading(false);
    }
  };

  const handleAbortConfirm = async (abortableItems: { objectKey: string; objectStatus?: string }[]) => {
    const objectKeys = abortableItems.map((item) => item.objectKey);
    logger.debug('Aborting workflows', objectKeys);

    setIsAbortLoading(true);
    try {
      const result = await abortWorkflows(objectKeys);
      logger.debug('Abort result', result);

      // Close the modal
      setIsAbortModalVisible(false);

      // Clear selection after aborting
      actions.setSelectedItems([]);
    } finally {
      setIsAbortLoading(false);
    }
  };

  const handleClaimReview = async () => {
    const client = generateClient();
    const selectedItems = collectionProps.selectedItems as MappedDocument[];
    const isSingleSelection = selectedItems.length === 1;

    // Claim reviews for all selected documents
    for (const item of selectedItems) {
      try {
        const result = await client.graphql({
          query: claimReview,
          variables: { objectKey: item.objectKey },
        });
        const claimData = (
          result as { data: { claimReview: { HITLReviewOwner: string; HITLReviewOwnerEmail: string; HITLStatus: string } } }
        ).data.claimReview;
        logger.debug('Claimed review for', item.objectKey, result);

        // Update the document in the list immediately
        setDocumentList((prevList) =>
          prevList.map((doc) =>
            doc.objectKey === item.objectKey
              ? {
                  ...doc,
                  hitlReviewOwner: claimData.HITLReviewOwner,
                  hitlReviewOwnerEmail: claimData.HITLReviewOwnerEmail,
                  hitlStatus: claimData.HITLStatus,
                }
              : doc,
          ),
        );
      } catch (e) {
        logger.error('Error claiming review', e);
      }
    }

    // Clear selection
    actions.setSelectedItems([]);

    // If single document selected, navigate to document details
    if (isSingleSelection) {
      const documentId = selectedItems[0].objectKey;
      logger.debug('Navigating to document details:', documentId);
      navigate(`/documents/${encodeURIComponent(documentId)}`);
    }
  };

  const handleReleaseReview = async () => {
    const client = generateClient();
    for (const item of collectionProps.selectedItems as MappedDocument[]) {
      try {
        const result = await client.graphql({
          query: releaseReview,
          variables: { objectKey: item.objectKey },
        });
        const releaseData = (result as { data: { releaseReview: { HITLStatus: string } } }).data.releaseReview;
        logger.debug('Released review for', item.objectKey, result);

        // Update the document in the list immediately
        setDocumentList((prevList) =>
          prevList.map((doc) =>
            doc.objectKey === item.objectKey
              ? {
                  ...doc,
                  hitlReviewOwner: '' as string,
                  hitlReviewOwnerEmail: '' as string,
                  hitlStatus: releaseData.HITLStatus,
                }
              : doc,
          ),
        );
      } catch (e) {
        logger.error('Error releasing review', e);
      }
    }

    // Clear selection
    actions.setSelectedItems([]);
  };

  return (
    <>
      <Table
        {...collectionProps}
        header={
          <DocumentsCommonHeader
            resourceName="Documents"
            documents={documents}
            selectedItems={collectionProps.selectedItems}
            totalItems={filteredDocumentList}
            updateTools={() => setToolsOpen(true)}
            loading={isDocumentsListLoading}
            setIsLoading={setIsDocumentsListLoading}
            periodsToLoad={periodsToLoad}
            setPeriodsToLoad={setPeriodsToLoad}
            customDateRange={customDateRange}
            setCustomDateRange={setCustomDateRange}
            onCustomDateRange={() => setIsDateRangeModalVisible(true)}
            getDocumentDetailsFromIds={getDocumentDetailsFromIds}
            downloadToExcel={() => {
              const exportData = filteredDocumentList.map((item) => ({
                ...item,
                configVersion: formatConfigVersionText(item.configVersion, versions),
              }));
              exportToExcel(exportData, 'Document-List');
            }}
            onReprocess={canWrite ? () => setIsReprocessModalVisible(true) : null}
            onDelete={canWrite ? () => setIsDeleteModalVisible(true) : null}
            onAbort={canWrite ? () => setIsAbortModalVisible(true) : null}
            onClaimReview={canReview ? handleClaimReview : null}
            onReleaseReview={isAdmin ? handleReleaseReview : null}
            currentUsername={currentUsername}
          />
        }
        columnDefinitions={COLUMN_DEFINITIONS_MAIN(versions)}
        items={items}
        loading={isDocumentsListLoading}
        loadingText="Loading documents"
        selectionType="multi"
        ariaLabels={SELECTION_LABELS}
        filter={
          <TextFilter
            {...filterProps}
            filteringAriaLabel="Filter documents"
            filteringPlaceholder="Find documents"
            countText={getFilterCounterText(filteredItemsCount ?? 0)}
          />
        }
        wrapLines={preferences.wrapLines}
        pagination={<Pagination {...paginationProps} ariaLabels={paginationLabels} />}
        preferences={<DocumentsPreferences preferences={preferences} setPreferences={setPreferences as (prefs: unknown) => void} />}
        trackBy={UNIQUE_TRACK_ID}
        visibleColumns={[KEY_COLUMN_ID, ...preferences.visibleContent]}
        resizableColumns
      />

      <DeleteDocumentModal
        visible={isDeleteModalVisible}
        onDismiss={() => setIsDeleteModalVisible(false)}
        onConfirm={handleDeleteConfirm}
        selectedItems={(collectionProps.selectedItems ?? []) as readonly { objectKey: string }[]}
        isLoading={isDeleteLoading}
      />

      <ReprocessDocumentModal
        visible={isReprocessModalVisible}
        onDismiss={() => setIsReprocessModalVisible(false)}
        onConfirm={handleReprocessConfirm}
        selectedItems={collectionProps.selectedItems}
        isLoading={isReprocessLoading}
      />

      <AbortWorkflowModal
        visible={isAbortModalVisible}
        onDismiss={() => setIsAbortModalVisible(false)}
        onConfirm={handleAbortConfirm}
        selectedItems={collectionProps.selectedItems}
        isLoading={isAbortLoading}
      />

      <DateRangeModal
        visible={isDateRangeModalVisible}
        onDismiss={() => setIsDateRangeModalVisible(false)}
        onApply={(dateRange: DateRange) => {
          setIsDateRangeModalVisible(false);
          setCustomDateRange(dateRange);
          localStorage.setItem('customDateRange', JSON.stringify(dateRange));
        }}
      />
    </>
  );
};

export default DocumentList;
