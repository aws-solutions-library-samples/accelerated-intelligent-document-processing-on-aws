// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React, { useEffect, useState } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { ConsoleLogger } from 'aws-amplify/utils';

import useDocumentsContext from '../../contexts/documents';
import useSettingsContext from '../../contexts/settings';
import useUserRole from '../../hooks/use-user-role';

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

const logger = new ConsoleLogger('documentDetails');

const DocumentDetails = (): React.JSX.Element => {
  const params = useParams();
  const navigate = useNavigate();

  // Get the objectKey from the wildcard route parameter '*'
  // This captures the full path including any embedded slashes (e.g., folder/filename.pdf)
  let objectKey = params['*'] ?? '';

  // Ensure we properly decode the objectKey from the URL parameter
  // It may be already decoded or still encoded depending on browser behavior with refreshes
  try {
    objectKey = decodeURIComponent(objectKey);
  } catch (e) {
    // If it fails, it might be already decoded
    logger.debug('Error decoding objectKey, using as is', e);
  }

  const { documents, getDocumentDetailsFromIds, setToolsOpen, deleteDocuments, reprocessDocuments, abortWorkflows } = useDocumentsContext();
  const { settings: _settings } = useSettingsContext();
  const { canWrite } = useUserRole();

  const [document, setDocument] = useState<MappedDocument | null>(null);
  const [isDeleteModalVisible, setIsDeleteModalVisible] = useState(false);
  const [isReprocessModalVisible, setIsReprocessModalVisible] = useState(false);
  const [isAbortModalVisible, setIsAbortModalVisible] = useState(false);
  const [isDeleteLoading, setIsDeleteLoading] = useState(false);
  const [isReprocessLoading, setIsReprocessLoading] = useState(false);
  const [isAbortLoading, setIsAbortLoading] = useState(false);

  const sendInitDocumentRequests = async (): Promise<void> => {
    const response = await getDocumentDetailsFromIds([objectKey]);
    logger.debug('document detail response', response);
    const documentsMap = mapDocumentsAttributes(response as unknown as { ObjectKey: string }[]) as MappedDocument[];
    const documentDetails = documentsMap[0];
    if (documentDetails) {
      setDocument(documentDetails);
    }
  };

  // Initial load
  useEffect(() => {
    if (!objectKey) {
      return () => {};
    }
    sendInitDocumentRequests();
    return () => {};
  }, [objectKey]);

  // Handle updates from subscription or document list context.
  // Rich data (from subscriptions — detected by having 'sections' key after mapping)
  // does a full replacement so reprocessed documents properly clear stale fields.
  // Lightweight list data (from listDocuments — no 'sections' key) preserves
  // existing rich fields to avoid wiping detail data.
  useEffect(() => {
    if (!objectKey || !documents?.length) {
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
  }, [documents, objectKey]);

  logger.debug('Document details render:', objectKey, document, documents);

  const handleDeleteConfirm = async () => {
    logger.debug('Deleting document', objectKey);

    setIsDeleteLoading(true);
    try {
      const result = await deleteDocuments([objectKey]);
      logger.debug('Delete result', result);

      // Navigate back to document list
      navigate(DOCUMENTS_PATH);
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
      if (document) {
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

  return (
    <>
      {document && (
        <DocumentPanel
          item={document}
          setToolsOpen={setToolsOpen}
          getDocumentDetailsFromIds={getDocumentDetailsFromIds}
          onDelete={canWrite ? handleDeleteClick : null}
          onReprocess={canWrite ? handleReprocessClick : null}
          onAbort={canWrite ? handleAbortClick : null}
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
