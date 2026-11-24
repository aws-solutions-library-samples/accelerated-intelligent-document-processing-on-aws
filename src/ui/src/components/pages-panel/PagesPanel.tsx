// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React, { useState } from 'react';
import { Alert, Box, ButtonDropdown, Container, Header, SpaceBetween, Table } from '@cloudscape-design/components';
import type { ButtonDropdownProps } from '@cloudscape-design/components';
import { useCollection } from '@cloudscape-design/collection-hooks';
import { defineMessages, FormattedMessage, useIntl, type IntlShape, type NoMessageValues, type TypedMessageDescriptor } from 'react-intl';
import { PageClassMismatch } from '../common/ClassMismatchIndicator';
import ClassNameText from '../common/ClassNameText';
import ClassConfidence, { compareClassConfidence } from '../common/ClassConfidence';
import { EMPTY_CLASSIFICATION_INDEX, type ClassificationIndex } from '../common/classification-comparison-utils';
import PageTextEditorModal from './PageTextEditorModal';
import usePageThumbnails, { THUMBNAIL_FAILURE_MESSAGES } from '../../hooks/use-page-thumbnails';
import { actionReasonMessages } from '../document-actions/action-reasons';
import { ColumnPreferences, fillTableColumns, useColumnPreferences } from '../common/detail-table-columns';

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

const messages = defineMessages<{
  title: NoMessageValues;
  thumbnailAlt: NoMessageValues;
  thumbnailTitle: NoMessageValues;
  noImage: NoMessageValues;
  viewText: NoMessageValues;
  editText: NoMessageValues;
  actionsAriaLabel: { readonly page: string };
  thumbnailsUnavailable: NoMessageValues;
  emptyTitle: NoMessageValues;
  emptyBody: NoMessageValues;
}>({
  title: {
    id: 'documents.pages.title',
    defaultMessage: 'Document Pages',
    description: 'Heading of the pages table on the document page; also names the table in its column preferences.',
  },
  thumbnailAlt: {
    id: 'documents.pages.thumbnailAlt',
    defaultMessage: 'Page thumbnail',
    description: "Alternative text of a page's thumbnail image.",
  },
  thumbnailTitle: {
    id: 'documents.pages.thumbnailTitle',
    defaultMessage: 'Click to view full size image',
    description: "Tooltip of a page's thumbnail, which opens the full-size image.",
  },
  noImage: {
    id: 'documents.pages.noImage',
    defaultMessage: 'No image',
    description: 'Shown in the Thumbnail column for a page without an image.',
  },
  viewText: {
    id: 'documents.pages.viewText',
    defaultMessage: 'View Page Text',
    description: "Item of a page row's menu that shows the page's text, read-only.",
  },
  editText: {
    id: 'documents.pages.editText',
    defaultMessage: 'Edit Page Text',
    description: "Item of a page row's menu that opens the page's text for editing.",
  },
  actionsAriaLabel: {
    id: 'documents.pages.actionsAriaLabel',
    defaultMessage: 'Actions for page {page}',
    description: "Screen-reader label of a page row's menu. {page} is the page id.",
  },
  thumbnailsUnavailable: {
    id: 'documents.pages.thumbnailsUnavailable',
    defaultMessage: 'Page thumbnails unavailable',
    description: 'Heading of the alert shown when the page thumbnails could not be loaded.',
  },
  emptyTitle: {
    id: 'documents.pages.empty.title',
    defaultMessage: 'No pages',
    description: 'Heading of the pages table when the document has no page.',
  },
  emptyBody: {
    id: 'documents.pages.empty.body',
    defaultMessage: 'This document has no pages.',
    description: 'Text of the pages table when the document has no page.',
  },
});

/** The column headers, keyed by column id; the ⚙ preferences name the columns with the same messages. */
const columnMessages = defineMessages<{
  id: NoMessageValues;
  class: NoMessageValues;
  classConfidence: NoMessageValues;
  thumbnail: NoMessageValues;
  actions: NoMessageValues;
}>({
  id: {
    id: 'documents.pages.column.id',
    defaultMessage: 'Page ID',
    description: 'Column header of the pages table: the page number.',
  },
  class: {
    id: 'documents.pages.column.class',
    defaultMessage: 'Class/Type',
    description: 'Column header of the pages table: the class the page was classified as.',
  },
  classConfidence: {
    id: 'documents.pages.column.classConfidence',
    defaultMessage: 'Class Confidence',
    description: "Column header of the pages table: the classifier's confidence in the page's class.",
  },
  thumbnail: {
    id: 'documents.pages.column.thumbnail',
    defaultMessage: 'Thumbnail',
    description: 'Column header of the pages table: a small image of the page.',
  },
  actions: {
    id: 'documents.pages.column.actions',
    defaultMessage: 'Actions',
    description: 'Column header of the pages table: the row menu.',
  },
});

/** The text in the UI's language; without `intl` (tests reading the column ids) the English source text. */
const labelOf = (message: TypedMessageDescriptor<NoMessageValues>, intl?: IntlShape): string =>
  intl ? intl.formatMessage(message) : String(message.defaultMessage);

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
const ThumbnailCell = ({ imageUrl }: { imageUrl?: string | null }): React.JSX.Element => {
  const intl = useIntl();
  return (
    <div style={{ width: '100px', height: '100px' }}>
      {imageUrl ? (
        <a href={imageUrl} target="_blank" rel="noopener noreferrer" style={{ cursor: 'pointer' }}>
          <img
            src={imageUrl}
            alt={intl.formatMessage(messages.thumbnailAlt)}
            style={{
              maxWidth: '100%',
              maxHeight: '100%',
              objectFit: 'contain',
              transition: 'transform 0.2s',
            }}
            title={intl.formatMessage(messages.thumbnailTitle)}
          />
        </a>
      ) : (
        <Box textAlign="center" color="inherit">
          <FormattedMessage {...messages.noImage} />
        </Box>
      )}
    </div>
  );
};

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
  const intl = useIntl();
  const noText = item.TextUri ? undefined : intl.formatMessage(actionReasonMessages.pageNoText);
  const editReason = pageTextEdit?.disabledReason ?? noText;
  const items: ButtonDropdownProps.Item[] = [
    { id: 'view-text', text: intl.formatMessage(messages.viewText), disabled: !!noText, disabledReason: noText },
    ...(pageTextEdit
      ? [{ id: 'edit-text', text: intl.formatMessage(messages.editText), disabled: !!editReason, disabledReason: editReason }]
      : []),
  ];
  return (
    <ButtonDropdown
      items={items}
      variant="inline-icon"
      expandToViewport
      ariaLabel={intl.formatMessage(messages.actionsAriaLabel, { page: item.Id })}
      onItemClick={({ detail }) => {
        if (detail.id === 'view-text') onView(item);
        else if (detail.id === 'edit-text') pageTextEdit?.open(String(item.Id));
      }}
    />
  );
};

/** The data columns a user may show or hide (⚙), in their order; each is named by its header's message. */
export const PAGE_COLUMNS = ['id', 'class', 'classConfidence', 'thumbnail'] as const;

/**
 * The pages table's columns, the ⋮ Actions column last. Their widths come from
 * fillTableColumns (common/detail-table-columns), as the sections table's do.
 */
export const pageColumnDefinitions = (
  thumbnailUrls: Record<string, string | null>,
  onView: (item: PageItem) => void,
  classificationIndex: ClassificationIndex,
  pageTextEdit?: PagesPanelProps['pageTextEdit'],
  intl?: IntlShape,
) => [
  {
    id: 'id',
    header: labelOf(columnMessages.id, intl),
    cell: (item: PageItem) => <IdCell item={item} />,
    sortingField: 'Id',
  },
  {
    id: 'class',
    header: labelOf(columnMessages.class, intl),
    cell: (item: PageItem) => <ClassCell item={item} classificationIndex={classificationIndex} />,
    sortingField: 'Class',
  },
  {
    id: 'classConfidence',
    header: labelOf(columnMessages.classConfidence, intl),
    // The classifier's confidence in Class, and the affordance for its reasoning
    // and ranked alternatives. Its own column rather than more text in
    // Class/Type: it aligns down the column, and it sorts — least-confident-first
    // is how a reviewer finds the pages worth a second look.
    cell: (item: PageItem) => (
      <ClassConfidence confidence={item.ClassConfidence} reason={item.ClassReason} candidates={item.ClassCandidates} />
    ),
    sortingComparator: (a: PageItem, b: PageItem) => compareClassConfidence(a.ClassConfidence, b.ClassConfidence),
  },
  {
    id: 'thumbnail',
    header: labelOf(columnMessages.thumbnail, intl),
    cell: (item: PageItem) => <ThumbnailCell imageUrl={thumbnailUrls[item.Id]} />,
  },
  {
    id: 'actions',
    header: labelOf(columnMessages.actions, intl),
    cell: (item: PageItem) => <ActionsCell item={item} onView={onView} pageTextEdit={pageTextEdit} />,
  },
];

const PagesPanel = ({
  pages,
  classificationIndex = EMPTY_CLASSIFICATION_INDEX,
  pageTextEdit = null,
}: PagesPanelProps): React.JSX.Element => {
  const intl = useIntl();
  const [viewing, setViewing] = useState<PageItem | null>(null);
  const { urls: thumbnailUrls, failure: thumbnailFailure } = usePageThumbnails(pages);
  const tableItems = pages || [];

  // Sorting via the design system's own collection hook rather than a hand-rolled
  // sort: it honours BOTH `sortingField` and `sortingComparator` columns and owns
  // the direction, so every column that declares a sort works. Starts unsorted,
  // so the default view keeps its existing order. `sortedItems` feeds the table
  // ONLY — `tableItems` still feeds the text viewer, which pages in document order.
  const { items: sortedItems, collectionProps } = useCollection(tableItems, { sorting: {} });
  const columnOptions = PAGE_COLUMNS.map((id) => ({ id, label: intl.formatMessage(columnMessages[id]) }));
  const { preferences, setPreferences, visibleColumns } = useColumnPreferences('document-pages-columns', columnOptions);

  return (
    <SpaceBetween size="l">
      <Container
        header={
          <Header variant="h2">
            <FormattedMessage {...messages.title} />
          </Header>
        }
      >
        <SpaceBetween size="s">
          {/* Signing page thumbnails is a server call now, and a refusal fails the
              whole batch — so without this the Thumbnail column is simply empty and
              says nothing about why. */}
          {thumbnailFailure && (
            <Alert
              type={thumbnailFailure === 'access-denied' ? 'warning' : 'error'}
              header={intl.formatMessage(messages.thumbnailsUnavailable)}
            >
              <FormattedMessage {...THUMBNAIL_FAILURE_MESSAGES[thumbnailFailure]} />
            </Alert>
          )}
          <Table
            columnDefinitions={fillTableColumns(
              pageColumnDefinitions(thumbnailUrls, setViewing, classificationIndex, pageTextEdit, intl),
              visibleColumns,
            )}
            visibleColumns={visibleColumns}
            preferences={
              <ColumnPreferences
                title={intl.formatMessage(messages.title)}
                options={columnOptions}
                preferences={preferences}
                setPreferences={setPreferences}
              />
            }
            items={sortedItems}
            {...collectionProps}
            variant="embedded"
            stickyHeader
            empty={
              <Box textAlign="center" color="inherit">
                <b>
                  <FormattedMessage {...messages.emptyTitle} />
                </b>
                <Box padding={{ bottom: 's' }} variant="p" color="inherit">
                  <FormattedMessage {...messages.emptyBody} />
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
