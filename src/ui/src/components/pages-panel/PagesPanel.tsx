// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React, { useState } from 'react';
import { Alert, Box, ButtonDropdown, Container, Header, SpaceBetween, Table } from '@cloudscape-design/components';
import type { ButtonDropdownProps } from '@cloudscape-design/components';
import { useCollection } from '@cloudscape-design/collection-hooks';
import { PageClassMismatch } from '../common/ClassMismatchIndicator';
import ClassNameText from '../common/ClassNameText';
import ClassConfidence, { compareClassConfidence } from '../common/ClassConfidence';
import { EMPTY_CLASSIFICATION_INDEX, type ClassificationIndex } from '../common/classification-comparison-utils';
import PageTextEditorModal from './PageTextEditorModal';
import usePageThumbnails, { THUMBNAIL_FAILURE_MESSAGES } from '../../hooks/use-page-thumbnails';
import { PAGE_NO_TEXT_REASON } from '../document-actions/action-reasons';

interface PageItem {
  Id: string;
  Class?: string | null;
  /** Confidence in Class (0-1). Absent/null = not scored — see ClassConfidence. */
  ClassConfidence?: number | null;
  /** The model's stated evidence for Class, when the prompt asked for one. */
  ClassReason?: string | null;
  /** Ranked alternatives from the classification call's confidence.mode: topk. */
  ClassCandidates?: ({ Class?: string | null; Probability?: number | null } | null)[] | null;
  ImageUri?: string;
  TextUri?: string;
  TextConfidenceUri?: string;
  OcrPageDataUri?: string;
}

interface PagesPanelProps {
  pages?: PageItem[];
  /**
   * Ground-truth-vs-predicted classification for this document, loaded once by
   * the document page. Empty (the default) when there is no ground truth, in
   * which case no class is annotated.
   */
  classificationIndex?: ClassificationIndex;
  /**
   * Edit Page Text, for a row's ⋮ menu: offered (with the reason it can't be used now),
   * or null when the user isn't offered it. The document page's actions own the dialog.
   */
  pageTextEdit?: { disabledReason?: string; open: (pageId: string) => void } | null;
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

/** A page row's ⋮ menu: View Page Text, and Edit Page Text where the user is offered it. */
const ActionsCell = ({
  item,
  onView,
  pageTextEdit,
}: {
  item: PageItem;
  onView: (item: PageItem) => void;
  pageTextEdit?: PagesPanelProps['pageTextEdit'];
}): React.JSX.Element => {
  const noText = item.TextUri ? undefined : PAGE_NO_TEXT_REASON;
  const editReason = pageTextEdit?.disabledReason ?? noText;
  const items: ButtonDropdownProps.Item[] = [
    { id: 'view-text', text: 'View Page Text', disabled: !!noText, disabledReason: noText },
    ...(pageTextEdit ? [{ id: 'edit-text', text: 'Edit Page Text', disabled: !!editReason, disabledReason: editReason }] : []),
  ];
  return (
    <ButtonDropdown
      items={items}
      variant="inline-icon"
      expandToViewport
      ariaLabel={`Actions for page ${item.Id}`}
      onItemClick={({ detail }) => {
        if (detail.id === 'view-text') onView(item);
        else if (detail.id === 'edit-text') pageTextEdit?.open(String(item.Id));
      }}
    />
  );
};

/**
 * The pages table's columns: narrow data columns, resizable, and the ⋮ Actions column
 * last, taking the rest of the table's width.
 */
export const pageColumnDefinitions = (
  thumbnailUrls: Record<string, string | null>,
  onView: (item: PageItem) => void,
  classificationIndex: ClassificationIndex,
  pageTextEdit?: PagesPanelProps['pageTextEdit'],
) => [
  {
    id: 'id',
    header: 'Page ID',
    cell: (item: PageItem) => <IdCell item={item} />,
    sortingField: 'Id',
    width: 90,
  },
  {
    id: 'class',
    header: 'Class/Type',
    cell: (item: PageItem) => <ClassCell item={item} classificationIndex={classificationIndex} />,
    sortingField: 'Class',
    width: 180,
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
    width: 110,
  },
  {
    id: 'thumbnail',
    header: 'Thumbnail',
    cell: (item: PageItem) => <ThumbnailCell imageUrl={thumbnailUrls[item.Id]} />,
    width: 130,
  },
  {
    id: 'actions',
    header: 'Actions',
    cell: (item: PageItem) => <ActionsCell item={item} onView={onView} pageTextEdit={pageTextEdit} />,
    // Fills the rest of the table, so its header stays on one line.
    minWidth: 100,
  },
];

const PagesPanel = ({
  pages,
  classificationIndex = EMPTY_CLASSIFICATION_INDEX,
  pageTextEdit = null,
}: PagesPanelProps): React.JSX.Element => {
  const [viewing, setViewing] = useState<PageItem | null>(null);
  const { urls: thumbnailUrls, failure: thumbnailFailure } = usePageThumbnails(pages);
  const tableItems = pages || [];

  // Sorting via the design system's own collection hook rather than a hand-rolled
  // sort: it honours BOTH `sortingField` and `sortingComparator` columns and owns
  // the direction, so every column that declares a sort works. Starts unsorted,
  // so the default view keeps its existing order. `sortedItems` feeds the table
  // ONLY — `tableItems` still feeds the text viewer, which pages in document order.
  const { items: sortedItems, collectionProps } = useCollection(tableItems, { sorting: {} });

  return (
    <SpaceBetween size="l">
      <Container header={<Header variant="h2">Document Pages</Header>}>
        <SpaceBetween size="s">
          {/* Signing page thumbnails is a server call now, and a refusal fails the
              whole batch — so without this the Thumbnail column is simply empty and
              says nothing about why. */}
          {thumbnailFailure && (
            <Alert type={thumbnailFailure === 'access-denied' ? 'warning' : 'error'} header="Page thumbnails unavailable">
              {THUMBNAIL_FAILURE_MESSAGES[thumbnailFailure]}
            </Alert>
          )}
          <Table
            columnDefinitions={pageColumnDefinitions(thumbnailUrls, setViewing, classificationIndex, pageTextEdit)}
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
        </SpaceBetween>
      </Container>

      {/* View Page Text: read-only. Editing is the document page's Edit Page Text. */}
      <PageTextEditorModal visible={!!viewing} pages={tableItems} initialPageId={viewing?.Id} onClose={() => setViewing(null)} />
    </SpaceBetween>
  );
};

export default PagesPanel;
