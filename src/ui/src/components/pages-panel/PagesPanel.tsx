// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React, { useState, useEffect } from 'react';
import {
  Box,
  Container,
  SpaceBetween,
  Table,
  Button,
  Header,
  FormField,
  StatusIndicator,
  Modal,
  Alert,
} from '@cloudscape-design/components';
import { useCollection } from '@cloudscape-design/collection-hooks';
import { generateClient } from '../../api/client-shim';
import { ConsoleLogger } from 'aws-amplify/utils';
import useSettingsContext from '../../contexts/settings';
import useUserRole from '../../hooks/use-user-role';
import { PageClassMismatch } from '../common/ClassMismatchIndicator';
import ClassNameText from '../common/ClassNameText';
import ClassConfidence, { compareClassConfidence } from '../common/ClassConfidence';
import { EMPTY_CLASSIFICATION_INDEX, type ClassificationIndex } from '../common/classification-comparison-utils';
import PageTextEditorModal from './PageTextEditorModal';
import { processChanges } from '../../graphql/generated';
import { useDocumentVersion } from '../../contexts/document-version';
import usePageThumbnails from '../../hooks/use-page-thumbnails';

const client = generateClient();
const logger = new ConsoleLogger('PagesPanel');

interface PageItem {
  Id: string;
  Class?: string | null;
  /** Confidence in Class (0-1). Absent/null = not scored — see ClassConfidence. */
  ClassConfidence?: number | null;
  /** The model's stated evidence for Class, when the prompt asked for one. */
  ClassReason?: string | null;
  /** Ranked alternatives from classification.confidence.mode: topk. */
  ClassCandidates?: ({ Class?: string | null; Probability?: number | null } | null)[] | null;
  ImageUri?: string;
  TextUri?: string;
  TextConfidenceUri?: string;
  OcrPageDataUri?: string;
  classReset?: boolean;
  textModified?: boolean;
  newTextUri?: string | null;
  newConfidenceUri?: string | null;
}

interface DocumentItem {
  hitlReviewOwner?: string;
  hitlReviewOwnerEmail?: string;
  hitlTriggered?: boolean;
  hitlStatus?: string;
  objectStatus?: string;
  ObjectKey?: string;
  objectKey?: string;
}

interface PagesPanelProps {
  pages?: PageItem[];
  documentItem?: DocumentItem;
  /**
   * Ground-truth-vs-predicted classification for this document, loaded once by
   * the document page. Empty (the default) when there is no ground truth, in
   * which case no class is annotated.
   */
  classificationIndex?: ClassificationIndex;
}

// Cell renderer components
const IdCell = ({ item }: { item: PageItem }): React.JSX.Element => <span>{item.Id}</span>;
// Class/Type, annotated with a mismatch alert when ground truth expects a
// different class for this page. Page ids are the same 1-based numbers the
// comparison index is keyed on.
const ClassCell = ({
  item,
  classificationIndex = EMPTY_CLASSIFICATION_INDEX,
}: {
  item: PageItem;
  classificationIndex?: ClassificationIndex;
}): React.JSX.Element => (
  <SpaceBetween direction="horizontal" size="xs">
    <ClassNameText>{item.Class || '-'}</ClassNameText>
    <PageClassMismatch index={classificationIndex} pageNumber={Number(item.Id)} predictedClass={item.Class} />
  </SpaceBetween>
);
const ThumbnailCell = ({ imageUrl }: { imageUrl?: string | null }): React.JSX.Element => (
  <div style={{ width: '100px', height: '100px' }}>
    {imageUrl ? (
      <a href={imageUrl} target="_blank" rel="noopener noreferrer" style={{ cursor: 'pointer' }}>
        <img
          src={imageUrl}
          alt="Page thumbnail"
          style={{
            maxWidth: '100%',
            maxHeight: '100%',
            objectFit: 'contain',
            transition: 'transform 0.2s',
          }}
          title="Click to view full size image"
        />
      </a>
    ) : (
      <Box textAlign="center" color="inherit">
        No image
      </Box>
    )}
  </div>
);

const ActionsCell = ({
  item,
  isEditMode,
  onViewEditClick,
}: {
  item: PageItem;
  isEditMode: boolean;
  onViewEditClick: (item: PageItem) => void;
}): React.JSX.Element =>
  item.TextUri ? (
    <Button onClick={() => onViewEditClick(item)}>{isEditMode ? 'Edit Page Text' : 'View Page Text'}</Button>
  ) : (
    <Box color="text-status-inactive">No text available</Box>
  );

// Edit mode: Class/Type column
const EditableClassCell = ({
  item,
  onResetClass,
  classificationIndex = EMPTY_CLASSIFICATION_INDEX,
}: {
  item: PageItem;
  onResetClass: (id: string) => void;
  classificationIndex?: ClassificationIndex;
}): React.JSX.Element => (
  <FormField>
    {item.Class ? (
      <SpaceBetween direction="horizontal" size="xs">
        <StatusIndicator>
          <ClassNameText>{item.Class}</ClassNameText>
        </StatusIndicator>
        <PageClassMismatch index={classificationIndex} pageNumber={Number(item.Id)} predictedClass={item.Class} />
        <Button iconName="close" variant="icon" ariaLabel="Reset classification" onClick={() => onResetClass(item.Id)} />
      </SpaceBetween>
    ) : (
      <StatusIndicator type="info">Unclassified</StatusIndicator>
    )}
  </FormField>
);

// Column definitions for view mode
const createViewColumnDefinitions = (
  thumbnailUrls: Record<string, string | null>,
  onViewEditClick: (item: PageItem) => void,
  classificationIndex: ClassificationIndex,
) => [
  {
    id: 'id',
    header: 'Page ID',
    cell: (item: PageItem) => <IdCell item={item} />,
    sortingField: 'Id',
    minWidth: 160,
    width: 160,
    isResizable: true,
  },
  {
    id: 'class',
    header: 'Class/Type',
    cell: (item: PageItem) => <ClassCell item={item} classificationIndex={classificationIndex} />,
    sortingField: 'Class',
    minWidth: 200,
    width: 200,
    isResizable: true,
  },
  {
    id: 'classConfidence',
    header: 'Class conf.',
    // The classifier's confidence in Class, and the affordance for its reasoning
    // and ranked alternatives. Its own column rather than more text in
    // Class/Type: it aligns down the column, and it sorts — least-confident-first
    // is how a reviewer finds the pages worth a second look.
    cell: (item: PageItem) => (
      <ClassConfidence confidence={item.ClassConfidence} reason={item.ClassReason} candidates={item.ClassCandidates} />
    ),
    sortingComparator: (a: PageItem, b: PageItem) => compareClassConfidence(a.ClassConfidence, b.ClassConfidence),
    minWidth: 140,
    width: 140,
    isResizable: true,
  },
  {
    id: 'thumbnail',
    header: 'Thumbnail',
    cell: (item: PageItem) => <ThumbnailCell imageUrl={thumbnailUrls[item.Id]} />,
    minWidth: 240,
    width: 240,
    isResizable: true,
  },
  {
    id: 'actions',
    header: 'Actions',
    cell: (item: PageItem) => <ActionsCell item={item} isEditMode={false} onViewEditClick={onViewEditClick} />,
    minWidth: 200,
    width: 200,
    isResizable: true,
  },
];

// Column definitions for edit mode
const createEditColumnDefinitions = (
  thumbnailUrls: Record<string, string | null>,
  onResetClass: (id: string) => void,
  onViewEditClick: (item: PageItem) => void,
  classificationIndex: ClassificationIndex,
) => [
  {
    id: 'id',
    header: 'Page ID',
    cell: (item: PageItem) => <IdCell item={item} />,
    sortingField: 'Id',
    minWidth: 160,
    width: 160,
    isResizable: true,
  },
  {
    id: 'class',
    header: 'Class/Type',
    cell: (item: PageItem) => <EditableClassCell item={item} onResetClass={onResetClass} classificationIndex={classificationIndex} />,
    minWidth: 250,
    width: 250,
    isResizable: true,
  },
  {
    id: 'classConfidence',
    header: 'Class conf.',
    // The classifier's confidence in Class, and the affordance for its reasoning
    // and ranked alternatives. Its own column rather than more text in
    // Class/Type: it aligns down the column, and it sorts — least-confident-first
    // is how a reviewer finds the pages worth a second look.
    cell: (item: PageItem) => (
      <ClassConfidence confidence={item.ClassConfidence} reason={item.ClassReason} candidates={item.ClassCandidates} />
    ),
    sortingComparator: (a: PageItem, b: PageItem) => compareClassConfidence(a.ClassConfidence, b.ClassConfidence),
    minWidth: 140,
    width: 140,
    isResizable: true,
  },
  {
    id: 'thumbnail',
    header: 'Thumbnail',
    cell: (item: PageItem) => <ThumbnailCell imageUrl={thumbnailUrls[item.Id]} />,
    minWidth: 240,
    width: 240,
    isResizable: true,
  },
  {
    id: 'actions',
    header: 'Actions',
    cell: (item: PageItem) => <ActionsCell item={item} isEditMode={true} onViewEditClick={onViewEditClick} />,
    minWidth: 200,
    width: 200,
    isResizable: true,
  },
];

const PagesPanel = ({ pages, documentItem, classificationIndex = EMPTY_CLASSIFICATION_INDEX }: PagesPanelProps): React.JSX.Element => {
  const [isEditMode, setIsEditMode] = useState(false);
  const [editedPages, setEditedPages] = useState<PageItem[]>([]);
  const [modifiedPageIds, setModifiedPageIds] = useState<Set<string>>(new Set());
  const [selectedPage, setSelectedPage] = useState<PageItem | null>(null);
  const [showModalEditor, setShowModalEditor] = useState(false);
  const [showPattern1Modal, setShowPattern1Modal] = useState(false);
  const [showConfirmModal, setShowConfirmModal] = useState(false);
  const [isProcessing, setIsProcessing] = useState(false);

  const { settings } = useSettingsContext();
  const { isReviewerOnly, canWrite, canReview } = useUserRole();
  // When viewing a past version, pin page images to that run's object versions
  // and disable editing (edits write to the current objects, not the snapshot).
  const { isHistorical } = useDocumentVersion();
  // Shared with the page-regrouping board, which needs the same signed thumbnails.
  const thumbnailUrls = usePageThumbnails(pages);

  // Edit Mode should be disabled for reviewers until they click Start Review (claim the document)
  const hasReviewOwner = !!(documentItem?.hitlReviewOwner || documentItem?.hitlReviewOwnerEmail);
  const hitlTriggered = !!documentItem?.hitlTriggered;

  // Check HITL status
  const hitlStatusLower = documentItem?.hitlStatus?.toLowerCase().replace(/\s+/g, '') || '';
  const isHitlCompleted = hitlStatusLower === 'completed' || hitlStatusLower === 'reviewcompleted';
  const isHitlSkipped = hitlStatusLower === 'skipped' || hitlStatusLower === 'reviewskipped';

  // Check if document is currently processing
  const processingStatuses = ['queued', 'running', 'processing', 'postprocessing', 'summarizing', 'evaluating'];
  const docStatus = documentItem?.objectStatus?.toLowerCase() || '';
  const isDocumentProcessing = processingStatuses.includes(docStatus);

  // Disable edit mode if:
  // - User has no write or review permissions (Viewer role), OR
  // - REVIEWER only: HITL triggered but not claimed, document processing, or HITL completed/skipped
  // Admins and Authors can always edit
  // - Viewing a historical version (read-only snapshot)
  const isEditModeDisabled =
    isHistorical ||
    (!canWrite && !canReview) ||
    (isReviewerOnly && ((hitlTriggered && !hasReviewOwner) || isDocumentProcessing || isHitlCompleted || isHitlSkipped));

  // Auto-exit edit mode when switching to a historical version, or (for
  // reviewers) when the document starts processing or HITL is completed/skipped.
  useEffect(() => {
    if (isHistorical && isEditMode) {
      setIsEditMode(false);
      return;
    }
    if (isReviewerOnly && isEditMode && (isDocumentProcessing || isHitlCompleted || isHitlSkipped)) {
      setIsEditMode(false);
    }
  }, [isHistorical, isReviewerOnly, isDocumentProcessing, isHitlCompleted, isHitlSkipped, isEditMode]);

  // Initialize edited pages when entering edit mode
  useEffect(() => {
    if (isEditMode && pages) {
      const pagesWithEditableFormat: PageItem[] = pages.map((page) => ({
        ...page,
        classReset: false,
        textModified: false,
        newTextUri: null as string | null,
        newConfidenceUri: null as string | null,
      }));
      setEditedPages(pagesWithEditableFormat);
      setModifiedPageIds(new Set());
    }
  }, [isEditMode, pages]);

  // Check if current pattern is Pattern-1
  const isPattern1 = () => {
    const pattern = (settings as Record<string, unknown>)?.IDPPattern as string | undefined;
    return pattern && pattern.toLowerCase().includes('pattern1');
  };

  // Handle Edit Pages button click
  const handleEditPagesClick = () => {
    if (isPattern1()) {
      setShowPattern1Modal(true);
    } else {
      setIsEditMode(true);
    }
  };

  // Handle reset class
  const handleResetClass = (pageId: string): void => {
    const updatedPages = editedPages.map((page) => {
      if (page.Id === pageId) {
        // Mark as modified
        setModifiedPageIds((prev) => new Set([...prev, pageId]));
        return {
          ...page,
          Class: null,
          classReset: true,
        };
      }
      return page;
    });
    setEditedPages(updatedPages);
  };

  // Handle view/edit page text
  const handleViewEditClick = (page: PageItem): void => {
    setSelectedPage(page);
    setShowModalEditor(true);
  };

  // Handle modal save
  const handleModalSave = (pageId: string | number | undefined, newTextUri: string | null, newConfidenceUri: string | null): void => {
    logger.info(`handleModalSave called: pageId=${pageId}, newTextUri=${newTextUri}, newConfidenceUri=${newConfidenceUri}`);

    // Mark page as text modified using functional update
    setModifiedPageIds((prev) => {
      const updated = new Set([...prev, String(pageId)]);
      logger.info(`Updated modifiedPageIds:`, Array.from(updated));
      return updated;
    });

    // Use functional update to ensure we have latest state
    setEditedPages((prevPages) => {
      logger.info(`Current editedPages length: ${prevPages.length}`);

      const updatedPages = prevPages.map((page) => {
        if (page.Id === pageId) {
          const updated = {
            ...page,
            textModified: true,
            newTextUri: newTextUri || page.TextUri,
            newConfidenceUri: newConfidenceUri || page.TextConfidenceUri,
          };
          logger.info(`Updated page ${pageId}:`, updated);
          return updated;
        }
        return page;
      });

      return updatedPages;
    });

    logger.info(`Page ${pageId} marked as modified`);
  };

  // Calculate impact of changes
  const calculateImpact = () => {
    let classResetCount = 0;
    let textModifiedCount = 0;

    modifiedPageIds.forEach((pageId) => {
      const page = editedPages.find((p) => p.Id === pageId);
      if (page) {
        if (page.classReset) classResetCount++;
        if (page.textModified) textModifiedCount++;
      }
    });

    return { classResetCount, textModifiedCount };
  };

  // Build modified pages payload
  const buildModifiedPagesPayload = () => {
    logger.info('Building modified pages payload...');
    logger.info(`modifiedPageIds (${modifiedPageIds.size} items):`, Array.from(modifiedPageIds));
    logger.info(`editedPages (${editedPages.length} items):`, editedPages);

    const payload = Array.from(modifiedPageIds)
      .map((pageId) => {
        const page = editedPages.find((p) => p.Id === pageId);
        logger.info(`Looking for page ${pageId}, found:`, page);

        if (!page) {
          logger.warn(`Page ${pageId} not found in editedPages`);
          return null;
        }

        const result = {
          pageId: parseInt(pageId, 10),
          textModified: page.textModified || false,
          classReset: page.classReset || false,
          newTextUri: page.newTextUri,
          newConfidenceUri: page.newConfidenceUri,
        };

        logger.info(`Built payload for page ${pageId}:`, result);
        logger.info(`Will include? textModified=${result.textModified}, classReset=${result.classReset}`);

        return result;
      })
      .filter((p): p is NonNullable<typeof p> => {
        const include = p != null && (p.textModified || p.classReset);
        if (p != null && !include) {
          logger.warn(`Filtering out page ${p.pageId} - no modifications detected`);
        }
        return !!include;
      });

    logger.info(`Final payload (${payload.length} pages):`, payload);
    return payload;
  };

  // Handle save and process changes
  const handleSaveChanges = async () => {
    if (modifiedPageIds.size === 0) {
      return;
    }

    setShowConfirmModal(true);
  };

  // Confirm and process changes
  const confirmSaveChanges = async () => {
    setIsProcessing(true);
    setShowConfirmModal(false);

    try {
      const objectKey = documentItem?.ObjectKey || documentItem?.objectKey;

      if (!objectKey) {
        throw new Error('Document object key is missing');
      }

      const modifiedPages = buildModifiedPagesPayload();

      logger.info(`Processing ${modifiedPages.length} modified pages`);
      logger.info('Modified pages payload:', JSON.stringify(modifiedPages));

      // Validate that we have changes to process
      if (!modifiedPages || modifiedPages.length === 0) {
        throw new Error('No valid page modifications to process');
      }

      const result = await client.graphql({
        query: processChanges,
        variables: {
          objectKey,
          modifiedSections: [], // Empty for page-only changes
          modifiedPages,
        },
      });

      const response = (result as { data: Record<string, unknown> }).data?.processChanges as
        | { success: boolean; message?: string }
        | undefined;

      if (!response?.success) {
        throw new Error(response?.message || 'Failed to process changes');
      }

      // Exit edit mode
      setIsEditMode(false);
      setEditedPages([]);
      setModifiedPageIds(new Set());

      alert('Page changes submitted for reprocessing!');
    } catch (error) {
      logger.error('Error processing changes:', error);
      alert(`Error processing changes: ${(error as Error).message}`);
    } finally {
      setIsProcessing(false);
    }
  };

  // Cancel edit mode
  const cancelEdit = () => {
    setIsEditMode(false);
    setEditedPages([]);
    setModifiedPageIds(new Set());
  };

  // Determine which columns and data to use
  const columnDefinitions = isEditMode
    ? createEditColumnDefinitions(thumbnailUrls, handleResetClass, handleViewEditClick, classificationIndex)
    : createViewColumnDefinitions(thumbnailUrls, handleViewEditClick, classificationIndex);

  const tableItems = isEditMode ? editedPages : pages || [];

  // Sorting via the design system's own collection hook rather than a hand-rolled
  // sort: it honours BOTH `sortingField` and `sortingComparator` columns and owns
  // the direction, so every column that declares a sort works. Starts unsorted,
  // so the default view keeps its existing order. `sortedItems` feeds the table
  // ONLY — `tableItems` still feeds everything else, which must stay in document
  // order however the table is sorted.
  const { items: sortedItems, collectionProps } = useCollection(tableItems, { sorting: {} });

  return (
    <SpaceBetween size="l">
      <Container
        header={
          <Header
            variant="h2"
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                {!isEditMode ? (
                  <Button variant="primary" iconName="edit" onClick={handleEditPagesClick} disabled={isEditModeDisabled}>
                    Edit Mode
                  </Button>
                ) : (
                  <>
                    <Button variant="link" onClick={cancelEdit} disabled={isProcessing}>
                      Cancel
                    </Button>
                    <Button
                      variant="primary"
                      iconName="external"
                      onClick={handleSaveChanges}
                      disabled={modifiedPageIds.size === 0 || isProcessing}
                      loading={isProcessing}
                    >
                      Process Changes
                    </Button>
                  </>
                )}
              </SpaceBetween>
            }
          >
            Document Pages
          </Header>
        }
      >
        <Table
          columnDefinitions={columnDefinitions}
          items={sortedItems}
          {...collectionProps}
          variant="embedded"
          resizableColumns
          stickyHeader
          empty={
            <Box textAlign="center" color="inherit">
              <b>No pages</b>
              <Box padding={{ bottom: 's' }} variant="p" color="inherit">
                This document has no pages.
              </Box>
            </Box>
          }
          wrapLines
        />
      </Container>

      {/* Page Text Editor Modal */}
      <PageTextEditorModal
        visible={showModalEditor}
        pages={tableItems}
        initialPageId={selectedPage?.Id}
        isReadOnly={!isEditMode}
        onSave={handleModalSave}
        onClose={() => {
          setShowModalEditor(false);
          setSelectedPage(null);
        }}
      />

      {/* Pattern-1 Information Modal */}
      <Modal
        visible={showPattern1Modal}
        onDismiss={() => setShowPattern1Modal(false)}
        header="Edit Mode - Pattern-1"
        footer={
          <Box float="right">
            <Button variant="primary" onClick={() => setShowPattern1Modal(false)}>
              Got it
            </Button>
          </Box>
        }
      >
        <SpaceBetween size="m">
          <Alert type="info" header="Feature Not Available for Pattern-1">
            <Box>
              The Edit Pages feature is currently available for <strong>Pattern-2</strong> and <strong>Pattern-3</strong> only.
            </Box>
          </Alert>

          <Box>
            <strong>Why is this different for Pattern-1?</strong>
          </Box>

          <Box>
            Pattern-1 uses <strong>Bedrock Data Automation (BDA)</strong> which has its own document processing approach that integrates
            directly with Amazon Bedrock&apos;s blueprints. Page-level modifications are managed differently in this pattern.
          </Box>

          <Box>
            <strong>Available alternatives for Pattern-1:</strong>
          </Box>

          <ul>
            <li>
              <strong>View Page Text</strong>: Use the &quot;View Page Text&quot; button to review page content
            </li>
            <li>
              <strong>Configuration</strong>: Adjust document classes and extraction rules in the Configuration tab
            </li>
            <li>
              <strong>Reprocess Document</strong>: Use the &quot;Reprocess&quot; button to run the document through the pipeline again
            </li>
          </ul>

          <Box>
            For fine-grained page control, consider using <strong>Pattern-2</strong> or <strong>Pattern-3</strong> for future documents.
          </Box>
        </SpaceBetween>
      </Modal>

      {/* Confirmation Modal */}
      <Modal
        visible={showConfirmModal}
        onDismiss={() => setShowConfirmModal(false)}
        header="Confirm Page Changes"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setShowConfirmModal(false)}>
                Cancel
              </Button>
              <Button variant="primary" onClick={confirmSaveChanges}>
                Confirm & Process
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="s">
          <Box>You are about to save changes to document pages and trigger selective reprocessing. This will:</Box>

          {(() => {
            const { classResetCount, textModifiedCount } = calculateImpact();
            return (
              <ul>
                {classResetCount > 0 && (
                  <li>
                    <strong>{classResetCount}</strong> page(s) will have their classification reset, triggering reclassification and
                    removing affected sections
                  </li>
                )}
                {textModifiedCount > 0 && (
                  <li>
                    <strong>{textModifiedCount}</strong> page(s) had text modifications, triggering re-extraction for affected sections
                  </li>
                )}
              </ul>
            );
          })()}

          <Box>The document will be reprocessed based on these changes.</Box>
        </SpaceBetween>
      </Modal>
    </SpaceBetween>
  );
};

export default PagesPanel;
