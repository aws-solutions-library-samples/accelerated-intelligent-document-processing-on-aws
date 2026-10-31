// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { ConsoleLogger } from 'aws-amplify/utils';

import useDocumentsContext from '../../contexts/documents';

import LoadState from '../common/LoadState';
import { documentFailedModel, documentMissingModel, loadingDocumentModel } from '../edit-screen/edit-mode-states';
import mapDocumentsAttributes from '../common/map-document-attributes';
import DeleteDocumentModal from '../common/DeleteDocumentModal';
import ReprocessDocumentModal from '../common/ReprocessDocumentModal';
import AbortWorkflowModal from '../common/AbortWorkflowModal';
import { DOCUMENTS_PATH } from '../../routes/constants';

import '@cloudscape-design/global-styles/index.css';

import DocumentPanel from '../document-panel';

interface MappedDocument {
  objectKey: string;
  objectStatus: string;
  [key: string]: unknown;
}

interface AbortableItem {
  objectKey: string;
  objectStatus?: string;
}

/** The page's first read of its document: until it settles, the page shows a spinner. */
type Read = { kind: 'loading' } | { kind: 'missing' } | { kind: 'failed'; error: unknown } | { kind: 'ready' };

const logger = new ConsoleLogger('documentDetails');

const DocumentDetails = (): React.JSX.Element => {
  const params = useParams();
  const navigate = useNavigate();

  // The objectKey is the wildcard route parameter '*', the full path including any
  // embedded slashes (e.g., folder/filename.pdf). The router has already decoded it once;
  // decoding it again would break a key that contains `%`.
  const objectKey = params['*'] ?? '';

  const { documents, getDocumentDetailsFromIds, deleteDocuments, reprocessDocuments, abortWorkflows } = useDocumentsContext();

  const [document, setDocument] = useState<MappedDocument | null>(null);
  const [read, setRead] = useState<Read>({ kind: 'loading' });
  // The latest read wins: one for a key the page has left is dropped.
  const readId = useRef(0);
  const [isDeleteModalVisible, setIsDeleteModalVisible] = useState(false);
  const [isReprocessModalVisible, setIsReprocessModalVisible] = useState(false);
  const [isAbortModalVisible, setIsAbortModalVisible] = useState(false);
  const [isDeleteLoading, setIsDeleteLoading] = useState(false);
  const [isReprocessLoading, setIsReprocessLoading] = useState(false);
  const [isAbortLoading, setIsAbortLoading] = useState(false);

  // The rich read (sections, pages, cost). A failed read is shown here with Retry, not
  // in the shared banner; a document that does not exist reads as nothing.
  const readDocument = useCallback(async (): Promise<void> => {
    const id = ++readId.current;
    setRead({ kind: 'loading' });
    let failure: { error: unknown } | null = null;
    const response = await getDocumentDetailsFromIds([objectKey], {
      reportErrors: false,
      onError: (_key, error) => {
        failure = { error };
      },
    });
    if (id !== readId.current) return;
    logger.debug('document detail response', response);
    const documentDetails = (mapDocumentsAttributes(response as unknown as { ObjectKey: string }[]) as MappedDocument[])[0];
    if (documentDetails) {
      setDocument(documentDetails);
      setRead({ kind: 'ready' });
    } else if (failure) {
      setRead({ kind: 'failed', error: (failure as { error: unknown }).error });
    } else {
      setRead({ kind: 'missing' });
    }
  }, [objectKey, getDocumentDetailsFromIds]);

  // Initial load, and again for each key the page opens.
  useEffect(() => {
    setDocument(null);
    if (!objectKey) return;
    void readDocument();
  }, [objectKey]);

  // Handle updates from subscription or document list context.
  // Rich data (from subscriptions — detected by having 'sections' key after mapping)
  // does a full replacement so reprocessed documents properly clear stale fields.
  // Lightweight list data (from listDocuments — no 'sections' key) preserves
  // existing rich fields to avoid wiping detail data.
  useEffect(() => {
    // List rows are partial: until the first rich read settles they would flash a
    // half-filled page, so they are ignored.
    if (!objectKey || !documents?.length || read.kind !== 'ready') {
      return;
    }

    const documentsFiltered = documents.filter((c) => c.ObjectKey === objectKey);
    if (documentsFiltered && documentsFiltered?.length) {
      const rawDoc = documentsFiltered[0] as unknown as Record<string, unknown>;
      const documentsMap = mapDocumentsAttributes([documentsFiltered[0]] as unknown as { ObjectKey: string }[]) as MappedDocument[];
      const incomingDoc = documentsMap[0];

      if (!document) {
        // No existing document yet — use whatever we got
        setDocument(incomingDoc);
        return;
      }

      // Detect rich data: subscription/getDocument responses include 'Sections'
      // (even if empty/null). Lightweight listDocuments responses never include it.
      const isRichData = 'Sections' in rawDoc;

      if (isRichData) {
        // Full replacement from subscription — allows clearing stale fields
        // (e.g., after reprocess). This is the common path during active processing.
        // However, protect against subscription events (e.g. claimReview/releaseReview)
        // that carry empty Sections/Pages overwriting data loaded by getDocument.
        if (JSON.stringify(document) !== JSON.stringify(incomingDoc)) {
          const existingSections = (document as Record<string, unknown>).sections as unknown[] | undefined;
          const incomingSections = (incomingDoc as Record<string, unknown>).sections as unknown[] | undefined;
          if (Array.isArray(existingSections) && existingSections.length > 0 && (!incomingSections || incomingSections.length === 0)) {
            // Incoming would wipe sections — overlay only changed scalar fields
            const merged = { ...document } as Record<string, unknown>;
            const incoming = incomingDoc as Record<string, unknown>;
            Object.keys(incoming).forEach((key) => {
              const val = incoming[key];
              if (val === null || val === undefined) return;
              if (Array.isArray(val) && val.length === 0) return;
              (merged as Record<string, unknown>)[key] = val;
            });
            logger.debug('Preserving sections from getDocument, merging subscription fields');
            setDocument(merged as MappedDocument);
          } else {
            logger.debug('Full document replacement from subscription data');
            setDocument(incomingDoc);
          }
        }
        return;
      }

      // Lightweight list data — preserve existing rich fields
      const merged = { ...document } as Record<string, unknown>;
      let hasChanges = false;

      for (const [key, newValue] of Object.entries(incomingDoc as Record<string, unknown>)) {
        const existingValue = (document as Record<string, unknown>)[key];

        // Skip if the new value is empty/missing but existing value has data.
        // This prevents lightweight list data from wiping rich detail fields.
        const newIsEmpty =
          newValue === undefined ||
          newValue === null ||
          (Array.isArray(newValue) && newValue.length === 0) ||
          (typeof newValue === 'object' && newValue !== null && !Array.isArray(newValue) && Object.keys(newValue).length === 0);
        const existingHasData =
          existingValue !== undefined &&
          existingValue !== null &&
          !(Array.isArray(existingValue) && existingValue.length === 0) &&
          !(
            typeof existingValue === 'object' &&
            existingValue !== null &&
            !Array.isArray(existingValue) &&
            Object.keys(existingValue).length === 0
          );

        if (newIsEmpty && existingHasData) {
          // Keep existing rich data — don't overwrite with empty
          continue;
        }

        if (JSON.stringify(existingValue) !== JSON.stringify(newValue)) {
          merged[key] = newValue;
          hasChanges = true;
        }
      }

      if (hasChanges) {
        logger.debug('Merging lightweight list update (preserving existing rich data)', merged);
        setDocument(merged as MappedDocument);
      }
    }
  }, [documents, objectKey, read.kind]);

  logger.debug('Document details render:', objectKey, document, documents);

  const handleDeleteConfirm = async () => {
    logger.debug('Deleting document', objectKey);

    setIsDeleteLoading(true);
    try {
      const result = await deleteDocuments([objectKey]);
      logger.debug('Delete result', result);

      // Back to the document list, once the document is gone (a failure is reported by the call).
      if (result !== false) navigate(DOCUMENTS_PATH);
    } finally {
      setIsDeleteLoading(false);
    }
  };

  // Function to show delete modal
  const handleDeleteClick = () => {
    setIsDeleteModalVisible(true);
  };

  // Function to show reprocess modal
  const handleReprocessClick = () => {
    setIsReprocessModalVisible(true);
  };

  // Function to handle reprocess confirmation
  const handleReprocessConfirm = async (version?: string, revision?: number) => {
    logger.debug('Reprocessing document', objectKey, 'with version', version, 'r', revision);
    setIsReprocessLoading(true);
    try {
      const result = await reprocessDocuments([objectKey], version, revision);
      logger.debug('Reprocess result', result);
      // Close the modal
      setIsReprocessModalVisible(false);

      // Immediately clear stale sections/pages so the UI doesn't show old data
      // while waiting for the backend pipeline to start and send subscription updates.
      // Not when the call failed (it reports why): the document is unchanged.
      if (document && result !== false) {
        setDocument({
          ...document,
          objectStatus: 'QUEUED',
          sections: [],
          pages: [],
          evaluationReportUri: '',
          summaryReportUri: '',
          ruleValidationResultUri: '',
          metering: null,
          confidenceAlertCount: 0,
        } as MappedDocument);
      }
    } finally {
      setIsReprocessLoading(false);
    }
  };

  // Function to show abort modal
  const handleAbortClick = () => {
    setIsAbortModalVisible(true);
  };

  // Function to handle abort confirmation
  const handleAbortConfirm = async (abortableItems: AbortableItem[]) => {
    const keys = abortableItems.map((item) => item.objectKey);
    logger.debug('Aborting workflow', keys);
    setIsAbortLoading(true);
    try {
      const result = await abortWorkflows(keys);
      logger.debug('Abort result', result);
      // Close the modal
      setIsAbortModalVisible(false);
    } finally {
      setIsAbortLoading(false);
    }
  };

  if (read.kind === 'loading' || (read.kind === 'ready' && !document)) return <LoadState state={loadingDocumentModel().state} />;
  if (read.kind === 'missing') return <LoadState state={documentMissingModel(false).state} />;
  if (read.kind === 'failed') return <LoadState state={documentFailedModel(read.error, () => void readDocument(), false).state} />;

  return (
    <>
      {document && (
        <DocumentPanel
          item={document}
          getDocumentDetailsFromIds={getDocumentDetailsFromIds}
          onDelete={handleDeleteClick}
          onReprocess={handleReprocessClick}
          onAbort={handleAbortClick}
        />
      )}

      <DeleteDocumentModal
        visible={isDeleteModalVisible}
        onDismiss={() => setIsDeleteModalVisible(false)}
        onConfirm={handleDeleteConfirm}
        selectedItems={document ? [document] : []}
        isLoading={isDeleteLoading}
      />

      <ReprocessDocumentModal
        visible={isReprocessModalVisible}
        onDismiss={() => setIsReprocessModalVisible(false)}
        onConfirm={handleReprocessConfirm}
        selectedItems={document ? [document] : []}
        isLoading={isReprocessLoading}
      />

      <AbortWorkflowModal
        visible={isAbortModalVisible}
        onDismiss={() => setIsAbortModalVisible(false)}
        onConfirm={handleAbortConfirm}
        selectedItems={document ? [document] : []}
        isLoading={isAbortLoading}
      />
    </>
  );
};

export default DocumentDetails;
