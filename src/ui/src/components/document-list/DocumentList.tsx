// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React, { useEffect, useState, useMemo } from 'react';
import { Table, Pagination, TextFilter, Box, Link, SpaceBetween } from '@cloudscape-design/components';
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
import useAppContext from '../../contexts/app';
import useUserRole from '../../hooks/use-user-role';

import mapDocumentsAttributes from '../common/map-document-attributes';
import { paginationLabels } from '../common/labels';
import useLocalStorage from '../common/local-storage';
import { exportToExcel } from '../common/download-func';
import DeleteDocumentModal from '../common/DeleteDocumentModal';
import ReprocessDocumentModal from '../common/ReprocessDocumentModal';
import AbortWorkflowModal from '../common/AbortWorkflowModal';
import DateRangeModal from '../common/DateRangeModal';
import { exportDocuments, triggerBrowserDownload } from '../document-panel/document-export';
import type { ExportErrorEntry, ExportProgress, ExportScope } from '../document-panel/document-export';
import { DownloadOptionsModal, DownloadProgressModal } from '../document-panel/DocumentDownloadModals';
import { claimReview, releaseReview } from '../../graphql/generated';

import type { MappedDocument } from './documents-table-config';
import {
  DocumentsPreferences,
  DocumentsCommonHeader,
  DocumentViewSelector,
  COLUMN_DEFINITIONS_MAIN,
  KEY_COLUMN_ID,
  UNIQUE_TRACK_ID,
  TEST_RUN_COLUMN_ID,
  SELECTION_LABELS,
  DEFAULT_PREFERENCES,
  DEFAULT_SORT_COLUMN,
} from './documents-table-config';
import { testExecutionsHref } from '../../routes/constants';

import { getFilterCounterText, TableEmptyState, TableNoMatchState } from '../common/table';
import useConfigurationVersions from '../../hooks/use-configuration-versions';
import { formatConfigVersionText } from '../test-studio/utils/configVersionUtils';

import '@cloudscape-design/global-styles/index.css';

const logger = new ConsoleLogger('DocumentList');

/**
 * Documents hydrated per round before a bulk export. Matches the exporter's own
 * fetch concurrency so a large selection cannot fan out unbounded.
 */
const HYDRATION_CHUNK_SIZE = 5;

/**
 * Width of the Find documents field. Roughly double Cloudscape's content-sized
 * default for a TextFilter sharing a horizontal container, which is cramped for
 * document keys — they are S3 object keys and often carry a folder prefix.
 */
const FILTER_WIDTH = '340px';

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
  const { settings } = useSettingsContext();
  const { currentCredentials } = useAppContext();
  const { isAdmin, isReviewerOnly, canWrite, canReview } = useUserRole();
  const navigate = useNavigate();

  // Bulk artifact download (ZIP) for the selected documents
  const [downloadScope, setDownloadScope] = useState<ExportScope | null>(null);
  const [pendingDownloadScope, setPendingDownloadScope] = useState<ExportScope | null>(null);
  const [includePageImages, setIncludePageImages] = useState(false);
  const [includeSourceDocument, setIncludeSourceDocument] = useState(false);
  const [isDownloadInProgress, setIsDownloadInProgress] = useState(false);
  const [isDownloadFinished, setIsDownloadFinished] = useState(false);
  const [downloadProgress, setDownloadProgress] = useState<ExportProgress | null>(null);
  const [downloadErrors, setDownloadErrors] = useState<ExportErrorEntry[]>([]);
  const [downloadDocumentCount, setDownloadDocumentCount] = useState(0);
  const [isDownloadCancelling, setIsDownloadCancelling] = useState(false);
  const [downloadAbortController, setDownloadAbortController] = useState<AbortController | null>(null);

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
    documentView,
    setDocumentView,
    latestTruncated,
    getDocumentDetailsFromIds,
    deleteDocuments,
    reprocessDocuments,
    abortWorkflows,
    hasListBeenLoaded,
  } = useDocumentsContext();

  const isTestView = documentView === 'TEST';

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
    if (isTestView) {
      // An empty test view usually means the date range predates the runs rather
      // than that there are none, so point at the run list instead of stopping at
      // "no documents".
      return (
        <Box margin={{ vertical: 'xs' }} textAlign="center" color="inherit">
          <SpaceBetween size="xxs">
            <div>
              <b>No Test Studio documents</b>
              <Box variant="p" color="inherit">
                No test run submitted documents in this time period. Widen the range with Load, or see{' '}
                <Link href={testExecutionsHref()}>Test Executions</Link>.
              </Box>
            </div>
          </SpaceBetween>
        </Box>
      );
    }
    return <TableEmptyState resourceName="Document" />;
  }, [isReviewerOnly, isTestView]);

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

  const handleReprocessConfirm = async (version?: string, revision?: number) => {
    const objectKeys = (collectionProps.selectedItems as MappedDocument[]).map((item) => item.objectKey);
    logger.debug('Reprocessing documents', objectKeys, 'with version', version, 'r', revision);

    setIsReprocessLoading(true);
    try {
      const result = await reprocessDocuments(objectKeys, version, revision);
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

  /**
   * Bulk artifact export for the selected rows. List rows carry no Sections/Pages
   * (those only come from the getDocument detail query), so the selection is
   * hydrated first and then handed to the shared exporter.
   */
  const startBulkDownload = async (scope: ExportScope) => {
    const selected = (collectionProps.selectedItems ?? []) as MappedDocument[];
    if (selected.length === 0) return;

    const controller = new AbortController();
    setDownloadScope(scope);
    setDownloadErrors([]);
    setIsDownloadCancelling(false);
    setDownloadProgress({
      completed: 0,
      total: 1,
      currentFile: `Loading details for ${selected.length} document${selected.length === 1 ? '' : 's'}…`,
      errors: [],
      documentsTotal: selected.length,
      documentsCompleted: 0,
    });
    setIsDownloadFinished(false);
    setIsDownloadInProgress(true);
    setDownloadAbortController(controller);

    try {
      const objectKeys = selected.map((item) => item.objectKey);

      // Hydration fans out one getDocument per row. It is chunked rather than
      // fired all at once so a 100-row selection does not become 100 simultaneous
      // API calls — the throttled failures would be invisible, since
      // getDocumentDetailsFromIds drops rejected fetches silently.
      const detailByKey = new Map<string, MappedDocument>();
      let hydrated = 0;
      for (let i = 0; i < objectKeys.length; i += HYDRATION_CHUNK_SIZE) {
        if (controller.signal.aborted) {
          throw new DOMException('Bulk export aborted', 'AbortError');
        }
        const chunk = objectKeys.slice(i, i + HYDRATION_CHUNK_SIZE);
        try {
          const details = await getDocumentDetailsFromIds(chunk);
          const mapped = mapDocumentsAttributes(details as unknown as { ObjectKey: string }[]) as MappedDocument[];
          mapped.forEach((doc) => detailByKey.set(doc.objectKey, doc));
        } catch (e) {
          // Recorded as a preflight error below via the missing-key check.
          logger.warn('Detail fetch failed for chunk', chunk, e);
        }
        hydrated += chunk.length;
        setDownloadProgress({
          completed: hydrated,
          total: objectKeys.length,
          currentFile: 'Loading document details…',
          errors: [],
          documentsTotal: objectKeys.length,
          phase: 'preparing',
        });
      }

      // A row whose details never arrived can only contribute its attributes, so
      // it must be reported — otherwise the archive silently omits that
      // document's predictions while reporting a clean export.
      const preflightErrors: ExportErrorEntry[] = selected
        .filter((row) => !detailByKey.has(row.objectKey))
        .map((row) => ({
          path: '(document details)',
          document: row.objectKey,
          message: 'Could not fetch document details — only document attributes were exported for this document',
        }));
      if (preflightErrors.length > 0) {
        logger.warn(
          'Hydration failed for documents',
          preflightErrors.map((e) => e.document),
        );
        setDownloadErrors(preflightErrors);
      }

      // Keep the table's ordering, falling back to the list row so a document
      // with a failed detail fetch still appears in the archive.
      const docs = selected.map((row) => detailByKey.get(row.objectKey) ?? row);

      const result = await exportDocuments(docs as unknown as Parameters<typeof exportDocuments>[0], settings, {
        scope,
        includePageImages: scope === 'all' ? includePageImages : false,
        includeSourceDocument: scope === 'all' ? includeSourceDocument : false,
        credentials: currentCredentials as Record<string, unknown>,
        signal: controller.signal,
        preflightErrors,
        onProgress: (p) => {
          setDownloadProgress(p);
          setDownloadErrors(p.errors);
        },
      });
      triggerBrowserDownload(result);
      setDownloadErrors(result.errors);
    } catch (err) {
      if ((err as Error)?.name === 'AbortError') {
        logger.info('Bulk document export cancelled by user');
      } else {
        logger.error('Bulk document export failed:', err);
        alert(`Download failed: ${(err as Error).message || 'Unknown error'}`);
      }
    } finally {
      setIsDownloadInProgress(false);
      setIsDownloadFinished(true);
      setIsDownloadCancelling(false);
      setDownloadAbortController(null);
    }
  };

  // Every bulk scope confirms first: the modal states how many documents are
  // included, warns on large selections, and offers the heavy-asset toggles.
  const handleDownloadSelected = (scope: ExportScope) => {
    const selectedCount = (collectionProps.selectedItems ?? []).length;
    if (selectedCount === 0) return;
    setDownloadDocumentCount(selectedCount);
    setPendingDownloadScope(scope);
  };

  const handleConfirmDownload = () => {
    const scope = pendingDownloadScope;
    setPendingDownloadScope(null);
    if (scope) void startBulkDownload(scope);
  };

  const handleCloseDownloadProgress = () => {
    setIsDownloadFinished(false);
    setDownloadProgress(null);
    setDownloadScope(null);
  };

  return (
    <>
      <Table
        {...collectionProps}
        header={
          <DocumentsCommonHeader
            resourceName={isTestView ? 'Test Studio Documents' : 'Documents'}
            documents={documents}
            selectedItems={collectionProps.selectedItems}
            totalItems={filteredDocumentList}
            // Latest is capped, so on a capped load the plain "(N)" would read as
            // the whole partition. "(N+)" says the list is a prefix of it. Only
            // overridden when nothing is selected — the "(selected/total)" form
            // carries more useful information than the marker does.
            counter={
              latestTruncated && (collectionProps.selectedItems ?? []).length === 0 ? `(${filteredDocumentList.length}+)` : undefined
            }
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
              exportToExcel(exportData, isTestView ? 'Test-Studio-Document-List' : 'Document-List');
            }}
            onDownloadSelected={handleDownloadSelected}
            isDownloadInProgress={isDownloadInProgress}
            onReprocess={canWrite ? () => setIsReprocessModalVisible(true) : null}
            onDelete={canWrite ? () => setIsDeleteModalVisible(true) : null}
            onAbort={canWrite ? () => setIsAbortModalVisible(true) : null}
            // Reprocessing or deleting a test run's documents would silently
            // invalidate the results scored against them (and the confidence
            // calibration derived from those results). Test Studio owns that
            // lifecycle — rerun or delete the run there instead.
            destructiveDisabledReason={
              isTestView ? 'Test Studio documents are managed by their test run — use Test Executions to rerun or delete a run' : null
            }
            onClaimReview={canReview ? handleClaimReview : null}
            onReleaseReview={isAdmin ? handleReleaseReview : null}
            currentUsername={currentUsername}
          />
        }
        columnDefinitions={COLUMN_DEFINITIONS_MAIN(versions, documentView)}
        items={items}
        loading={isDocumentsListLoading}
        loadingText="Loading documents"
        selectionType="multi"
        ariaLabels={SELECTION_LABELS}
        filter={
          // Source first, then search: the segmented control chooses which set of
          // documents is in the table, and the text filter narrows within it.
          <SpaceBetween size="xs" direction="horizontal" alignItems="center">
            <DocumentViewSelector documentView={documentView} setDocumentView={setDocumentView} disabled={isDocumentsListLoading} />
            {/* TextFilter fills its container, and a horizontal SpaceBetween sizes
                children to their content — so it needs an explicit width here or it
                collapses to roughly half of what it had as the sole filter child. */}
            <div style={{ width: FILTER_WIDTH }}>
              <TextFilter
                {...filterProps}
                filteringAriaLabel="Filter documents"
                filteringPlaceholder="Find documents"
                countText={getFilterCounterText(filteredItemsCount ?? 0)}
              />
            </div>
          </SpaceBetween>
        }
        wrapLines={preferences.wrapLines}
        pagination={<Pagination {...paginationProps} ariaLabels={paginationLabels} />}
        preferences={<DocumentsPreferences preferences={preferences} setPreferences={setPreferences as (prefs: unknown) => void} />}
        trackBy={UNIQUE_TRACK_ID}
        // Test Run is force-included in the test view rather than offered in
        // Preferences: the visibleContent preference is one shared localStorage
        // entry, so a column only meaningful in one view would otherwise need a
        // second entry to avoid an unselectable column in the other.
        visibleColumns={[KEY_COLUMN_ID, ...(isTestView ? [TEST_RUN_COLUMN_ID] : []), ...preferences.visibleContent].filter(
          (id, i, all) => all.indexOf(id) === i,
        )}
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

      <DownloadOptionsModal
        visible={pendingDownloadScope !== null}
        scope={pendingDownloadScope ?? 'all'}
        documentCount={downloadDocumentCount}
        includePageImages={includePageImages}
        includeSourceDocument={includeSourceDocument}
        onIncludePageImagesChange={setIncludePageImages}
        onIncludeSourceDocumentChange={setIncludeSourceDocument}
        onConfirm={handleConfirmDownload}
        onDismiss={() => setPendingDownloadScope(null)}
      />

      <DownloadProgressModal
        visible={(isDownloadInProgress || isDownloadFinished) && downloadScope !== null}
        progress={downloadProgress}
        errors={downloadErrors}
        isFinished={isDownloadFinished}
        isCancelling={isDownloadCancelling}
        onCancel={() => {
          setIsDownloadCancelling(true);
          downloadAbortController?.abort();
        }}
        onClose={handleCloseDownloadProgress}
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
