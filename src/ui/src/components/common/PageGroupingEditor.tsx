// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Re-group a packet's pages into the right sections, by dragging them.
 *
 * Built for the motivating case: a document whose packet split put pages
 * in the wrong sections, on a test set carrying annotations that must not be lost. The
 * grouping is ground truth — `split_document.page_indices` is what doc-split scoring
 * reads — so this is an annotation editor, not a layout tool.
 *
 * Persistence-free by design: the caller supplies `onSave`, because the two surfaces
 * write to different places (a test-set baseline vs a processed document's record) while
 * needing identical rules and identical UI. Validation lives in `section-grouping.ts`,
 * shared with the same two surfaces.
 *
 * ## Seven decisions, each with a reason
 *
 * **A page can be opened at readable size.** Deciding which section a page belongs to
 * means reading the page, and the card shows it at 112px — not enough to tell an invoice
 * from a field ticket. Offered as a menu item rather than a click on the card, so it does
 * not compete with the drag gesture, and free: pages already render at 1200px wide, so
 * the preview shows what is in memory rather than fetching anything.
 *
 * **The dragged page rides a `DragOverlay`, not its own transform.** Each column scrolls
 * its own pages (`overflowY: auto`) inside a row that scrolls sideways (`overflowX:
 * auto`), and a transformed child is clipped by a scrolling ancestor. Moving the card in
 * place therefore made it vanish the instant it left its own column — which is every
 * cross-section drag, the only drag that does anything. The overlay is `position: fixed`
 * and portaled to the body, so it is outside both clips; the source card stays put and
 * dims to mark where the page came from.
 *
 * **Columns hold their position while editing; order settles on save.** Sections are
 * stored in document order, because doc-split scoring takes a group's index from its list
 * position. Applying that sort to the *live* board instead made columns swap places under
 * the cursor: drop page 1 into the next section and that section now starts at page 1, so
 * it sorts left and the board reshuffles mid-edit. The draft is sorted on open and again
 * in `onSave`, and left alone in between.
 *
 * **Sections sit side by side, not stacked.** The hard part of a large packet is not
 * scroll length, it is that the page you are moving and the section you are moving it to
 * must both be on screen — otherwise the drag has to be held through a scroll. Columns
 * keep every section visible and make each drag short. Each column scrolls its own
 * pages.
 *
 * **Multi-select, because a bad split is normally a run.** When the classifier puts pages
 * 5-9 in the wrong section that is one mistake, and moving it should be one action rather
 * than five. Selection is by checkbox, with shift-click for a range.
 *
 * **Pages within a section are ordered, and the order is editable.** An earlier version
 * treated a section as a set and sorted on save, on the belief that position carried no
 * meaning. It does: `split_accuracy_with_order` compares `page_indices` by **list**
 * equality and the graded packet score is half Kendall's Tau over each page's position
 * (see the header of `section-grouping.ts` for the three consumers). So the board renders
 * stored order and never re-sorts it.
 *
 * **Drop targets are cards, not just columns — no `@dnd-kit/sortable`.** Each card is a
 * droppable as well as a draggable, so a drop onto a card inserts before it. That yields
 * an insert position both within a column and across columns from the machinery already
 * here, without `SortableContext` and its container-transfer edge cases. Dropping on the
 * column *strip* instead inserts in ascending position: "exactly here" versus "where it
 * belongs", which keeps the ordinary contiguous-packet move free of surprises.
 *
 * **Dragging is never the only route.** Every page also has a "Move to" menu, and
 * selection is by checkbox rather than click-to-select. A drag-only interface is unusable
 * with a keyboard or a screen reader even with dnd-kit's KeyboardSensor (wired up here),
 * and this is the one screen an annotator cannot route around — the same lesson as the
 * locate button in FormFieldRenderer, where a mouse gesture was the sole path to a
 * capability.
 */

import React, { useMemo, useState } from 'react';
import { createPortal } from 'react-dom';
import { defineMessages, FormattedMessage, useIntl, type IntlShape, type NoMessageValues } from 'react-intl';
import {
  DndContext,
  DragEndEvent,
  DragOverlay,
  DragStartEvent,
  KeyboardSensor,
  PointerSensor,
  closestCenter,
  useDraggable,
  useDroppable,
  useSensor,
  useSensors,
} from '@dnd-kit/core';
import {
  Alert,
  Badge,
  Box,
  Button,
  ButtonDropdown,
  Checkbox,
  Container,
  Header,
  Modal,
  Select,
  SpaceBetween,
  Spinner,
} from '@cloudscape-design/components';
import type { SelectProps } from '@cloudscape-design/components';

import type { ConfigClassOption } from './config-class-options';
import {
  GroupedSection,
  insertPageIds,
  isAscending,
  nudgePageIds,
  sortPageIds,
  sortSectionsByFirstPage,
  validateGrouping,
} from './section-grouping';
import type { GroupingIssue } from './section-grouping';

const messages = defineMessages<{
  pageLabel: { readonly id: number };
  movePage: { readonly id: number };
  movePageWithSelection: { readonly id: number; readonly others: number };
  drag: { readonly id: number };
  dragWithSelection: { readonly id: number; readonly others: number };
  selectPage: { readonly id: number };
  viewFullPage: NoMessageValues;
  orderWithin: NoMessageValues;
  moveEarlier: NoMessageValues;
  moveLater: NoMessageValues;
  moveToSection: NoMessageValues;
  sectionOption: { readonly id: string };
  sectionOptionWithClass: { readonly id: string; readonly class: string };
  sortSection: { readonly id: string };
  deleteSection: { readonly id: string };
  sectionHeader: { readonly id: string };
  chooseClass: NoMessageValues;
  customOrder: NoMessageValues;
  dropHere: NoMessageValues;
  invalidHeader: NoMessageValues;
  summary: { readonly pages: number; readonly sections: number };
  selected: { readonly count: number };
  clearSelection: NoMessageValues;
  addSection: NoMessageValues;
  expand: NoMessageValues;
  draggingPages: { readonly count: number };
  cancel: NoMessageValues;
  defaultSave: NoMessageValues;
  defaultTitle: NoMessageValues;
  close: NoMessageValues;
  notRendered: NoMessageValues;
}>({
  pageLabel: {
    id: 'common.pageGrouping.pageLabel',
    defaultMessage: 'Page {id}',
    description:
      "A page on the Edit Page Grouping board: its thumbnail's alternative text, the label under it while it is dragged alone, and the title and image text of its full-size view. {id} is the page's number.",
  },
  movePage: {
    id: 'common.pageGrouping.movePage',
    defaultMessage: 'Move page {id}',
    description: "Label, for screen readers, of the menu button on a page of the Edit Page Grouping board. {id} is the page's number.",
  },
  movePageWithSelection: {
    id: 'common.pageGrouping.movePageWithSelection',
    defaultMessage: '{others, plural, one {Move page {id} and # other selected page} other {Move page {id} and # other selected pages}}',
    description:
      "Label, for screen readers, of the menu button on a page of the Edit Page Grouping board when the page is selected with others, which move with it. {id} is the page's number; {others} is how many other pages are selected.",
  },
  drag: {
    id: 'common.pageGrouping.drag',
    defaultMessage: 'Page {id}, drag to reorder it or move it to another section',
    description:
      "Label, for screen readers, of a page's thumbnail on the Edit Page Grouping board, which is dragged to move the page. {id} is the page's number.",
  },
  dragWithSelection: {
    id: 'common.pageGrouping.dragWithSelection',
    defaultMessage:
      '{others, plural, one {Page {id}, drag to move it and # other selected page} other {Page {id}, drag to move it and # other selected pages}}',
    description:
      "Label, for screen readers, of a page's thumbnail on the Edit Page Grouping board when the page is selected with others, which move with it. {id} is the page's number; {others} is how many other pages are selected.",
  },
  selectPage: {
    id: 'common.pageGrouping.selectPage',
    defaultMessage: 'Select page {id}',
    description:
      "Label, for screen readers, of the checkbox that selects a page on the Edit Page Grouping board. {id} is the page's number.",
  },
  viewFullPage: {
    id: 'common.pageGrouping.viewFullPage',
    defaultMessage: 'View full page',
    description: "Item of a page's menu on the Edit Page Grouping board: shows the page at full size.",
  },
  orderWithin: {
    id: 'common.pageGrouping.orderWithin',
    defaultMessage: 'Order within this section',
    description:
      "Item of a page's menu on the Edit Page Grouping board that holds Move earlier and Move later, which move the page within its section.",
  },
  moveEarlier: {
    id: 'common.pageGrouping.moveEarlier',
    defaultMessage: 'Move earlier',
    description: "Item of a page's menu on the Edit Page Grouping board: moves the page one place earlier in its section.",
  },
  moveLater: {
    id: 'common.pageGrouping.moveLater',
    defaultMessage: 'Move later',
    description: "Item of a page's menu on the Edit Page Grouping board: moves the page one place later in its section.",
  },
  moveToSection: {
    id: 'common.pageGrouping.moveToSection',
    defaultMessage: 'Move to section',
    description: "Item of a page's menu on the Edit Page Grouping board that lists the other sections the page can move to.",
  },
  sectionOption: {
    id: 'common.pageGrouping.sectionOption',
    defaultMessage: 'Section {id}',
    description:
      "A section with no class, in the Move to section list of a page's menu on the Edit Page Grouping board. {id} is the section's number.",
  },
  sectionOptionWithClass: {
    id: 'common.pageGrouping.sectionOptionWithClass',
    defaultMessage: 'Section {id} ({class})',
    description:
      "A section in the Move to section list of a page's menu on the Edit Page Grouping board. {id} is the section's number; {class} is its document class, shown as is.",
  },
  sortSection: {
    id: 'common.pageGrouping.sortSection',
    defaultMessage: 'Sort section {id} pages in document order',
    description:
      "Label, for screen readers, of the button on a section's column of the Edit Page Grouping board that puts the section's pages back in document order. {id} is the section's number.",
  },
  deleteSection: {
    id: 'common.pageGrouping.deleteSection',
    defaultMessage: 'Delete section {id}',
    description:
      "Label, for screen readers, of the button that deletes an empty section from the Edit Page Grouping board. {id} is the section's number.",
  },
  sectionHeader: {
    id: 'common.pageGrouping.sectionHeader',
    defaultMessage: 'Section {id}',
    description: "Heading of a section's column on the Edit Page Grouping board. {id} is the section's number.",
  },
  chooseClass: {
    id: 'common.pageGrouping.chooseClass',
    defaultMessage: 'Choose a document class',
    description: "Placeholder of the class picker on a section's column of the Edit Page Grouping board.",
  },
  customOrder: {
    id: 'common.pageGrouping.customOrder',
    defaultMessage: 'Custom page order',
    description: "Badge on a section's column of the Edit Page Grouping board when the section's pages are not in document order.",
  },
  dropHere: {
    id: 'common.pageGrouping.dropHere',
    defaultMessage: 'Drop a page here.',
    description: "Shown in a section's column of the Edit Page Grouping board while the section has no pages.",
  },
  invalidHeader: {
    id: 'common.pageGrouping.invalidHeader',
    defaultMessage: 'This grouping is not valid yet',
    description: 'Heading of the alert above the Edit Page Grouping board that lists the problems with the grouping as a whole.',
  },
  summary: {
    id: 'common.pageGrouping.summary',
    defaultMessage: '{pages, plural, one {# page} other {# pages}} · {sections, plural, one {# section} other {# sections}}',
    description:
      "Badge above the Edit Page Grouping board. {pages} is the number of the document's pages; {sections} is the number of sections on the board.",
  },
  selected: {
    id: 'common.pageGrouping.selected',
    defaultMessage:
      '{count, plural, one {# page selected — moving any one moves all of them} other {# pages selected — moving any one moves all of them}}',
    description: 'Shown above the Edit Page Grouping board while pages are selected. {count} is the number of selected pages.',
  },
  clearSelection: {
    id: 'common.pageGrouping.clearSelection',
    defaultMessage: 'Clear selection',
    description: 'Link button above the Edit Page Grouping board that unselects every selected page.',
  },
  addSection: {
    id: 'common.pageGrouping.addSection',
    defaultMessage: 'Add section',
    description: 'Button above the Edit Page Grouping board that adds an empty section.',
  },
  expand: {
    id: 'common.pageGrouping.expand',
    defaultMessage: 'Expand',
    description: 'Button above the Edit Page Grouping board, when the board is shown in place on its screen, that opens it in a dialog.',
  },
  draggingPages: {
    id: 'common.pageGrouping.draggingPages',
    defaultMessage: '{count} pages',
    description:
      'Label under the page being dragged on the Edit Page Grouping board when the selected pages move with it. {count} is how many pages move, two or more.',
  },
  cancel: {
    id: 'common.pageGrouping.cancel',
    defaultMessage: 'Cancel',
    description: 'Button of the Edit Page Grouping board that closes it without saving.',
  },
  defaultSave: {
    id: 'common.pageGrouping.defaultSave',
    defaultMessage: 'Save grouping',
    description: "The Edit Page Grouping board's save button, when the screen that shows the board gives the button no label of its own.",
  },
  defaultTitle: {
    id: 'common.pageGrouping.defaultTitle',
    defaultMessage: 'Edit Page Grouping',
    description: "Title of the Edit Page Grouping board's dialog, when the screen that opens it gives no title of its own.",
  },
  close: {
    id: 'common.pageGrouping.close',
    defaultMessage: 'Close',
    description: "Button that closes a page's full-size view, opened from the Edit Page Grouping board.",
  },
  notRendered: {
    id: 'common.pageGrouping.notRendered',
    defaultMessage: 'This page has not finished rendering.',
    description:
      "Shown beside a spinner in a page's full-size view, opened from the Edit Page Grouping board, while the page's image is not ready.",
  },
});

/** The problems `validateGrouping` reports, worded one whole message per code by `groupingIssueText`. */
const issueMessages = defineMessages<{
  noId: NoMessageValues;
  duplicateId: { readonly id: string };
  noPages: NoMessageValues;
  pageClaimed: { readonly pageId: number; readonly sectionId: string };
  notInDocument: { readonly count: number; readonly pageIds: string };
  pagesNotInSection: { readonly count: number; readonly pageIds: string };
}>({
  noId: {
    id: 'common.sectionGrouping.noId',
    defaultMessage: 'A section has no id.',
    description: 'A problem with the grouping, listed in the alert above the Edit Page Grouping board.',
  },
  duplicateId: {
    id: 'common.sectionGrouping.duplicateId',
    defaultMessage: "Section id ''{id}'' is used by more than one section.",
    description:
      "A problem shown on a section's column of the Edit Page Grouping board. {id} is the section id that more than one section uses.",
  },
  noPages: {
    id: 'common.sectionGrouping.noPages',
    defaultMessage: 'This section has no pages. Move a page into it, or delete the section.',
    description: "A problem shown on a section's column of the Edit Page Grouping board while the section has no pages.",
  },
  pageClaimed: {
    id: 'common.sectionGrouping.pageClaimed',
    defaultMessage: 'Page {pageId} is already in section {sectionId}.',
    description:
      "A problem shown on a section's column of the Edit Page Grouping board when another section already holds one of its pages. {pageId} is the page's number; {sectionId} is the number of the section that holds it.",
  },
  notInDocument: {
    id: 'common.sectionGrouping.notInDocument',
    defaultMessage: '{count, plural, one {Page {pageIds} not in this document.} other {Pages {pageIds} not in this document.}}',
    description:
      "A problem shown on a section's column of the Edit Page Grouping board when the section holds pages the document does not have. {count} is how many; {pageIds} lists their numbers, separated by commas.",
  },
  pagesNotInSection: {
    id: 'common.sectionGrouping.pagesNotInSection',
    defaultMessage:
      '{count, plural, one {Page {pageIds} is not in any section. Every page has to belong to one.} other {Pages {pageIds} are not in any section. Every page has to belong to one.}}',
    description:
      'A problem with the grouping, listed in the alert above the Edit Page Grouping board, when pages of the document are in no section. {count} is how many; {pageIds} lists their numbers, separated by commas.',
  },
});

/** A problem `validateGrouping` found, in the reader's language. */
const groupingIssueText = (issue: GroupingIssue, intl: IntlShape): string => {
  switch (issue.code) {
    case 'noId':
      return intl.formatMessage(issueMessages.noId);
    case 'duplicateId':
      return intl.formatMessage(issueMessages.duplicateId, { id: issue.sectionId });
    case 'noPages':
      return intl.formatMessage(issueMessages.noPages);
    case 'pageClaimed':
      return intl.formatMessage(issueMessages.pageClaimed, { pageId: issue.pageId, sectionId: issue.sectionId });
    case 'notInDocument':
      return intl.formatMessage(issueMessages.notInDocument, { count: issue.pageIds.length, pageIds: issue.pageIds.join(', ') });
    case 'pagesNotInSection':
      return intl.formatMessage(issueMessages.pagesNotInSection, { count: issue.pageIds.length, pageIds: issue.pageIds.join(', ') });
  }
};

/** One page of the document, in the caller's own numbering. */
export interface GroupingPage {
  id: number;
  /** Thumbnail. Absent while still rendering, or when no preview is available. */
  imageUri?: string | null;
}

export interface PageGroupingEditorProps {
  pages: GroupingPage[];
  /** Current grouping. Page ids must be drawn from `pages`. */
  sections: GroupedSection[];
  /** Classes the deployment defines. Empty hides the class control. */
  classOptions?: ConfigClassOption[];
  canChangeClass?: boolean;
  /**
   * What saving will do on this surface — preserved field values, a reprocess, whichever.
   * Required, because the consequence differs per surface and is the thing a reviewer
   * most needs to know before committing.
   */
  consequence: React.ReactNode;
  saveLabel?: string;
  isSaving?: boolean;
  onSave: (sections: GroupedSection[]) => void | Promise<void>;
  /**
   * A second way to save, beside the primary one (the document page's Save and
   * Re-extract). Disabled like the primary save while the grouping is invalid or
   * unchanged, and also while `disabledReason` gives a reason for the draft.
   */
  secondarySave?: {
    label: string;
    disabledReason?: (draft: GroupedSection[]) => string | undefined;
    onSave: (sections: GroupedSection[]) => void | Promise<void>;
  };
  onCancel: () => void;
  /**
   * `inline` (the default) renders the board in place, with an Expand button that opens
   * it in a modal. `modal` opens it straight in the modal, with no Expand, and dismissing
   * the modal cancels the edit (Edit Page Grouping on the edit page and the document page).
   */
  presentation?: 'inline' | 'modal';
  /** The dialog's title. */
  title?: string;
}

/**
 * Column page-strip height. The dialog trades height for width: its columns are about
 * 40% shorter than a full-height board, so the dialog fits a laptop screen with its
 * footer in view, and the dialog is about 40% wider, so more sections sit side by side.
 */
const INLINE_COLUMN_HEIGHT = 320;
const EXPANDED_COLUMN_HEIGHT = 340;
/** The dialog's width: 40% wider than a Cloudscape `large` modal (820px); capped to the viewport. */
const MODAL_WIDTH = 1150;
/** The dialog body's minimum width: three 260px section columns and the gaps between them. */
const MODAL_MIN_WIDTH = 784;

/**
 * The id for a section the reviewer adds: one past every id in `sections`.
 *
 * Callers pass the sections the board opened with, the caller's current ones and the
 * draft, so an id deleted in this edit is never handed out again. A save keeps each sent section's stored fields
 * under the id it was sent with, so a new section that reused a deleted one's id would
 * silently inherit that section's fields and label source (#1895).
 */
const nextSectionId = (sections: GroupedSection[]): string => {
  const numeric = sections.map((s) => Number.parseInt(s.sectionId, 10)).filter((n) => Number.isFinite(n));
  return String((numeric.length > 0 ? Math.max(...numeric) : 0) + 1);
};

const CARD_WIDTH = 112;

/** The thumbnail itself, with no drag wiring — shared by the card and the drag overlay. */
const PageThumb = ({ page }: { page: GroupingPage }): React.JSX.Element => {
  const intl = useIntl();
  return page.imageUri ? (
    <img
      src={page.imageUri}
      alt={intl.formatMessage(messages.pageLabel, { id: page.id })}
      style={{ width: '100%', display: 'block', borderRadius: '2px' }}
    />
  ) : (
    <Box textAlign="center" padding="s">
      <Spinner />
    </Box>
  );
};

/** A page thumbnail: selectable, draggable, a drop target, and movable without dragging. */
const PageCard = ({
  page,
  sectionId,
  isSelected,
  selectionSize,
  isFirstInSection,
  isLastInSection,
  otherSections,
  onToggleSelect,
  onMove,
  onNudge,
  onPreview,
}: {
  page: GroupingPage;
  sectionId: string;
  isSelected: boolean;
  selectionSize: number;
  isFirstInSection: boolean;
  isLastInSection: boolean;
  otherSections: GroupedSection[];
  onToggleSelect: (pageId: number, viaShift: boolean) => void;
  onMove: (pageId: number, toSectionId: string) => void;
  onNudge: (pageId: number, direction: -1 | 1) => void;
  onPreview: (pageId: number) => void;
}): React.JSX.Element => {
  const intl = useIntl();
  const { attributes, listeners, setNodeRef, isDragging } = useDraggable({
    id: `page-${page.id}`,
    data: { pageId: page.id, fromSectionId: sectionId },
  });
  // Also a drop target, which is what gives a drop an insert *position* rather than only a
  // destination section — within a column and across columns alike.
  const { setNodeRef: setDropRef, isOver } = useDroppable({
    id: `before-page-${page.id}`,
    data: { sectionId, beforePageId: page.id },
  });

  // Dragging one page of a selection carries the whole selection, so say so.
  const movesWithSelection = isSelected && selectionSize > 1;
  const others = selectionSize - 1;
  const moveLabel = movesWithSelection
    ? intl.formatMessage(messages.movePageWithSelection, { id: page.id, others })
    : intl.formatMessage(messages.movePage, { id: page.id });

  return (
    <div
      ref={(node) => {
        setNodeRef(node);
        setDropRef(node);
      }}
      style={{
        /* No transform here on purpose: DragOverlay carries the page while it is in
           flight, because a transformed child cannot escape this column's own
           `overflowY: auto`. See the note at the top of the file. The card stays put
           and dims, marking where the page came from. */
        opacity: isDragging ? 0.4 : 1,
        border: `2px solid ${isSelected ? '#0073bb' : '#d5dbdb'}`,
        /* A left edge while hovered, because a drop here inserts *before* this page —
           a whole-card highlight would not say which side. */
        borderLeft: isOver && !isDragging ? '4px solid #0073bb' : undefined,
        borderRadius: '4px',
        padding: '4px',
        background: isSelected ? 'rgba(0, 115, 187, 0.06)' : '#ffffff',
        width: `${CARD_WIDTH}px`,
      }}
    >
      <div
        {...attributes}
        {...listeners}
        style={{ cursor: 'grab' }}
        aria-label={
          movesWithSelection
            ? intl.formatMessage(messages.dragWithSelection, { id: page.id, others })
            : intl.formatMessage(messages.drag, { id: page.id })
        }
      >
        <PageThumb page={page} />
      </div>
      <SpaceBetween direction="horizontal" size="xxs" alignItems="center">
        {/* A checkbox rather than click-to-select: it is reachable by keyboard, it does
            not fight the drag sensor for the same gesture, and it matches how selection
            works everywhere else in this app. Shift extends from the last one touched. */}
        <span
          onClickCapture={(event) => {
            if (event.shiftKey) {
              event.preventDefault();
              event.stopPropagation();
              onToggleSelect(page.id, true);
            }
          }}
        >
          <Checkbox
            checked={isSelected}
            onChange={() => onToggleSelect(page.id, false)}
            ariaLabel={intl.formatMessage(messages.selectPage, { id: page.id })}
          >
            {page.id}
          </Checkbox>
        </span>
        <ButtonDropdown
          variant="icon"
          ariaLabel={moveLabel}
          /* Portals the menu instead of rendering it inside the section column, which is
             a scrolling `overflow: auto` box: the menu is taller than the column, so its
             last item was clipped away, and it inherited the column's width, wrapping
             every label onto two lines. Same failure as the drag preview before it moved
             to a body-level overlay — a positioned element cannot escape a scrolling
             ancestor by z-index alone. */
          expandToViewport
          /* Reordering has to be reachable without a pointer: a drag-only route would put
             this section's page order — a scored field — out of reach of a keyboard or
             screen-reader user. Grouped so "earlier/later" reads as position and the
             section list reads as destination. */
          items={[
            /* First, because deciding where a page belongs usually means reading it, and
               a 112px thumbnail is not enough to tell an invoice from a field ticket.
               The detail is already loaded — pages render at PDF_RENDER_WIDTH (1200px)
               and the document view presigns the pipeline's full-size page image — so
               this shows what is in memory rather than fetching anything. */
            { id: '__view', text: intl.formatMessage(messages.viewFullPage) },
            {
              id: '__reorder',
              text: intl.formatMessage(messages.orderWithin),
              items: [
                { id: '__earlier', text: intl.formatMessage(messages.moveEarlier), disabled: isFirstInSection },
                { id: '__later', text: intl.formatMessage(messages.moveLater), disabled: isLastInSection },
              ],
            },
            ...(otherSections.length > 0
              ? [
                  {
                    id: '__move',
                    text: intl.formatMessage(messages.moveToSection),
                    items: otherSections.map((s) => ({
                      id: s.sectionId,
                      text: s.documentClass
                        ? intl.formatMessage(messages.sectionOptionWithClass, { id: s.sectionId, class: s.documentClass })
                        : intl.formatMessage(messages.sectionOption, { id: s.sectionId }),
                    })),
                  },
                ]
              : []),
          ]}
          onItemClick={({ detail }) => {
            if (detail.id === '__view') onPreview(page.id);
            else if (detail.id === '__earlier') onNudge(page.id, -1);
            else if (detail.id === '__later') onNudge(page.id, 1);
            else onMove(page.id, detail.id);
          }}
        />
      </SpaceBetween>
    </div>
  );
};

/** One section as a column: its class, its pages, and a drop target. */
const SectionColumn = ({
  section,
  pages,
  errors,
  classOptions,
  canChangeClass,
  otherSections,
  selectedPageIds,
  columnHeight,
  onClassChange,
  onDelete,
  onMove,
  onNudge,
  onPreview,
  onSortPages,
  onToggleSelect,
}: {
  section: GroupedSection;
  pages: GroupingPage[];
  errors: GroupingIssue[];
  classOptions: ConfigClassOption[];
  canChangeClass: boolean;
  otherSections: GroupedSection[];
  selectedPageIds: Set<number>;
  columnHeight: number;
  onClassChange: (sectionId: string, value: string) => void;
  onDelete: (sectionId: string) => void;
  onMove: (pageId: number, toSectionId: string) => void;
  onNudge: (pageId: number, direction: -1 | 1) => void;
  onPreview: (pageId: number) => void;
  onSortPages: (sectionId: string) => void;
  onToggleSelect: (pageId: number, viaShift: boolean) => void;
}): React.JSX.Element => {
  const intl = useIntl();
  const { setNodeRef, isOver } = useDroppable({ id: `section-${section.sectionId}`, data: { sectionId: section.sectionId } });
  const selected = classOptions.find((o) => o.value === section.documentClass);
  // A section whose pages do not run in document order is legitimate — a packet can be
  // assembled out of order — but it looks like a bug unless it is labelled.
  const customOrder = section.pageIds.length > 1 && !isAscending(section.pageIds);

  return (
    <div style={{ minWidth: '260px', maxWidth: '260px', flex: '0 0 auto' }}>
      <Container
        header={
          <Header
            variant="h3"
            actions={
              <SpaceBetween direction="horizontal" size="xxs">
                {customOrder && (
                  <Button
                    iconName="undo"
                    variant="icon"
                    ariaLabel={intl.formatMessage(messages.sortSection, { id: section.sectionId })}
                    onClick={() => onSortPages(section.sectionId)}
                  />
                )}
                <Button
                  iconName="remove"
                  variant="icon"
                  ariaLabel={intl.formatMessage(messages.deleteSection, { id: section.sectionId })}
                  /* Only an empty section can go: deleting one with pages would orphan
                     them, and deleting one with field values should be deliberate. Drag
                     the pages out first — validateGrouping's empty-section error says
                     exactly that. */
                  disabled={section.pageIds.length > 0}
                  onClick={() => onDelete(section.sectionId)}
                />
              </SpaceBetween>
            }
          >
            <FormattedMessage {...messages.sectionHeader} values={{ id: section.sectionId }} />
          </Header>
        }
      >
        <SpaceBetween size="s">
          {classOptions.length > 0 && (
            <Select
              selectedOption={selected ?? (section.documentClass ? { label: section.documentClass, value: section.documentClass } : null)}
              options={classOptions}
              disabled={!canChangeClass}
              placeholder={intl.formatMessage(messages.chooseClass)}
              onChange={({ detail }: { detail: SelectProps.ChangeDetail }) =>
                onClassChange(section.sectionId, detail.selectedOption.value ?? '')
              }
            />
          )}

          {customOrder && (
            <Badge color="blue">
              <FormattedMessage {...messages.customOrder} />
            </Badge>
          )}

          <div
            ref={setNodeRef}
            style={{
              display: 'flex',
              flexWrap: 'wrap',
              alignContent: 'flex-start',
              gap: '8px',
              height: `${columnHeight}px`,
              overflowY: 'auto',
              padding: '8px',
              borderRadius: '4px',
              border: `2px dashed ${isOver ? '#0073bb' : '#d5dbdb'}`,
              background: isOver ? 'rgba(0, 115, 187, 0.06)' : 'transparent',
            }}
          >
            {pages.length === 0 ? (
              <Box color="text-body-secondary" fontSize="body-s" padding="xs">
                <FormattedMessage {...messages.dropHere} />
              </Box>
            ) : (
              pages.map((page, index) => (
                <PageCard
                  key={page.id}
                  page={page}
                  sectionId={section.sectionId}
                  isSelected={selectedPageIds.has(page.id)}
                  selectionSize={selectedPageIds.size}
                  isFirstInSection={index === 0}
                  isLastInSection={index === pages.length - 1}
                  otherSections={otherSections}
                  onNudge={onNudge}
                  onPreview={onPreview}
                  onToggleSelect={onToggleSelect}
                  onMove={onMove}
                />
              ))
            )}
          </div>

          {errors.length > 0 && (
            <Alert type="error">
              <SpaceBetween size="xxs">
                {errors.map((issue) => (
                  <span key={JSON.stringify(issue)}>{groupingIssueText(issue, intl)}</span>
                ))}
              </SpaceBetween>
            </Alert>
          )}
        </SpaceBetween>
      </Container>
    </div>
  );
};

const PageGroupingEditor = ({
  pages,
  sections,
  classOptions = [],
  canChangeClass = true,
  consequence,
  saveLabel,
  isSaving = false,
  onSave,
  secondarySave,
  onCancel,
  presentation = 'inline',
  title,
}: PageGroupingEditorProps): React.JSX.Element => {
  const intl = useIntl();
  const asModal = presentation === 'modal';
  // Sorted once, on open. NOT re-sorted as the draft changes — see the stable-order note
  // at the top of the file.
  const [draft, setDraft] = useState<GroupedSection[]>(() => sortSectionsByFirstPage(sections));
  // The sections the board opened with, kept even if the caller's `sections` change while
  // it is open: an id deleted in this edit must never be handed to an added section.
  const [openedSections] = useState(sections);
  const [selectedPageIds, setSelectedPageIds] = useState<Set<number>>(new Set());
  const [lastTouchedPageId, setLastTouchedPageId] = useState<number | null>(null);
  const [isExpanded, setIsExpanded] = useState(asModal);
  const [activePageId, setActivePageId] = useState<number | null>(null);
  const [previewPageId, setPreviewPageId] = useState<number | null>(null);

  // A small distance threshold so a click on the thumbnail is a click, not a 0px drag.
  const sensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 4 } }), useSensor(KeyboardSensor));

  const pageById = useMemo(() => new Map(pages.map((p) => [p.id, p])), [pages]);
  const availablePageIds = useMemo(() => pages.map((p) => p.id), [pages]);
  const validation = useMemo(() => validateGrouping(draft, availablePageIds), [draft, availablePageIds]);
  // Canonical form on both sides. Equivalent to comparing the raw draft today, but it
  // stops being so the moment display order and stored order can diverge — which is now
  // the case, since the board no longer re-sorts as you edit.
  const isChanged = useMemo(
    () => JSON.stringify(sortSectionsByFirstPage(sections)) !== JSON.stringify(sortSectionsByFirstPage(draft)),
    [sections, draft],
  );

  const toggleSelect = (pageId: number, viaShift: boolean) => {
    setSelectedPageIds((prev) => {
      const next = new Set(prev);
      if (viaShift && lastTouchedPageId !== null) {
        // Range over DOCUMENT order, not over the order sections happen to be in: a run
        // of mis-assigned pages is contiguous in the document, which is the whole reason
        // range selection helps here.
        const [from, to] = [lastTouchedPageId, pageId].sort((a, b) => a - b);
        availablePageIds.filter((id) => id >= from && id <= to).forEach((id) => next.add(id));
      } else if (next.has(pageId)) {
        next.delete(pageId);
      } else {
        next.add(pageId);
      }
      return next;
    });
    setLastTouchedPageId(pageId);
  };

  /** What a gesture on `pageId` acts on: the whole selection when it is part of it. */
  const movingSet = (pageId: number): number[] => (selectedPageIds.has(pageId) ? [...selectedPageIds] : [pageId]);

  /**
   * Move `pageId` (or the selection) into `toSectionId`, before `beforePageId` when given.
   *
   * Without an anchor the block lands in ascending position, so an ordinary move into
   * another section still reads in document order; with one it lands exactly there, which
   * is how a manual order gets authored.
   */
  const movePages = (pageId: number, toSectionId: string, beforePageId?: number) => {
    const moving = movingSet(pageId);
    setDraft((prev) =>
      prev.map((section) =>
        section.sectionId === toSectionId
          ? { ...section, pageIds: insertPageIds(section.pageIds, moving, beforePageId) }
          : { ...section, pageIds: section.pageIds.filter((id) => !moving.includes(id)) },
      ),
    );
    // Clear afterwards: a selection that outlives its move is a trap, because the next
    // drag would silently carry pages the reviewer had forgotten were selected.
    setSelectedPageIds(new Set());
    setLastTouchedPageId(null);
  };

  /** Reorder within the section the page already belongs to. */
  const nudgePage = (pageId: number, direction: -1 | 1) => {
    const moving = movingSet(pageId);
    setDraft((prev) =>
      prev.map((section) =>
        section.pageIds.includes(pageId) ? { ...section, pageIds: nudgePageIds(section.pageIds, moving, direction) } : section,
      ),
    );
  };

  /** Put one section's pages back into document order, without touching the others. */
  const sortSectionPages = (sectionId: string) =>
    setDraft((prev) => prev.map((s) => (s.sectionId === sectionId ? { ...s, pageIds: sortPageIds(s.pageIds) } : s)));

  const handleDragStart = ({ active }: DragStartEvent) =>
    setActivePageId((active.data.current as { pageId?: number } | undefined)?.pageId ?? null);

  const handleDragEnd = ({ active, over }: DragEndEvent) => {
    setActivePageId(null);
    if (!over) return;
    const pageId = (active.data.current as { pageId?: number } | undefined)?.pageId;
    // A card droppable carries `beforePageId`; the column strip carries only `sectionId`.
    const target = over.data.current as { sectionId?: string; beforePageId?: number } | undefined;
    if (pageId === undefined || !target?.sectionId) return;
    movePages(pageId, target.sectionId, target.beforePageId);
  };

  const addSection = () =>
    setDraft((prev) => [
      ...prev,
      { sectionId: nextSectionId([...openedSections, ...sections, ...prev]), documentClass: null, pageIds: [] },
    ]);
  const deleteSection = (sectionId: string) => setDraft((prev) => prev.filter((s) => s.sectionId !== sectionId));
  const changeClass = (sectionId: string, value: string) =>
    setDraft((prev) => prev.map((s) => (s.sectionId === sectionId ? { ...s, documentClass: value } : s)));

  const columnHeight = isExpanded ? EXPANDED_COLUMN_HEIGHT : INLINE_COLUMN_HEIGHT;

  const activeDragPage = activePageId === null ? undefined : pageById.get(activePageId);
  // Matches what movePages will actually do, so the overlay cannot promise a different
  // move from the one that lands.
  const draggingCount = activePageId !== null && selectedPageIds.has(activePageId) ? selectedPageIds.size : 1;

  const body = (
    <SpaceBetween size="m">
      <Alert type="info">{consequence}</Alert>

      {validation.document.length > 0 && (
        <Alert type="error" header={intl.formatMessage(messages.invalidHeader)}>
          <SpaceBetween size="xxs">
            {validation.document.map((issue) => (
              <span key={JSON.stringify(issue)}>{groupingIssueText(issue, intl)}</span>
            ))}
          </SpaceBetween>
        </Alert>
      )}

      <SpaceBetween direction="horizontal" size="xs" alignItems="center">
        <Badge color={validation.isValid ? 'green' : 'red'}>
          <FormattedMessage {...messages.summary} values={{ pages: pages.length, sections: draft.length }} />
        </Badge>
        {selectedPageIds.size > 0 && (
          <>
            <Box fontSize="body-s" color="text-status-info">
              <FormattedMessage {...messages.selected} values={{ count: selectedPageIds.size }} />
            </Box>
            <Button variant="link" onClick={() => setSelectedPageIds(new Set())}>
              <FormattedMessage {...messages.clearSelection} />
            </Button>
          </>
        )}
        <Button iconName="add-plus" onClick={addSection}>
          <FormattedMessage {...messages.addSection} />
        </Button>
        {!isExpanded && !asModal && (
          <Button iconName="expand" onClick={() => setIsExpanded(true)}>
            <FormattedMessage {...messages.expand} />
          </Button>
        )}
      </SpaceBetween>

      <DndContext
        sensors={sensors}
        collisionDetection={closestCenter}
        onDragStart={handleDragStart}
        onDragEnd={handleDragEnd}
        onDragCancel={() => setActivePageId(null)}
      >
        {/* Horizontal, so the page being moved and its destination are both on screen.
            Scrolls sideways only once there are more sections than fit. */}
        <div data-page-grouping-columns style={{ display: 'flex', gap: '12px', overflowX: 'auto', paddingBottom: '8px' }}>
          {draft.map((section) => (
            <SectionColumn
              key={section.sectionId}
              section={section}
              /* Stored order, NOT sorted — the order is the section's reading order and is
                 scored. Sorting here is how authored order used to become invisible. */
              pages={section.pageIds.map((id) => pageById.get(id)).filter((p): p is GroupingPage => Boolean(p))}
              errors={validation.bySection[section.sectionId] ?? []}
              classOptions={classOptions}
              canChangeClass={canChangeClass}
              otherSections={draft.filter((s) => s.sectionId !== section.sectionId)}
              selectedPageIds={selectedPageIds}
              columnHeight={columnHeight}
              onClassChange={changeClass}
              onDelete={deleteSection}
              onMove={movePages}
              onNudge={nudgePage}
              onPreview={setPreviewPageId}
              onSortPages={sortSectionPages}
              onToggleSelect={toggleSelect}
            />
          ))}
        </div>

        {/* Portaled to the body because a column has `overflowY: auto` and the row has
            `overflowX: auto`, so anything rendered inside them is clipped at their edges —
            which is every cross-section drag, the only drag that does anything. The
            overlay is `position: fixed`, and the portal keeps it out of reach of a
            containing block the Cloudscape modal might establish in the expanded view. */}
        {createPortal(
          <DragOverlay zIndex={9999} dropAnimation={null}>
            {activeDragPage ? (
              <div
                data-page-grouping-overlay
                style={{
                  border: '2px solid #0073bb',
                  borderRadius: '4px',
                  padding: '4px',
                  background: '#ffffff',
                  width: `${CARD_WIDTH}px`,
                  boxShadow: '0 4px 12px rgba(0, 7, 22, 0.35)',
                  cursor: 'grabbing',
                }}
              >
                <PageThumb page={activeDragPage} />
                <Box textAlign="center" fontSize="body-s">
                  {draggingCount > 1 ? (
                    <FormattedMessage {...messages.draggingPages} values={{ count: draggingCount }} />
                  ) : (
                    <FormattedMessage {...messages.pageLabel} values={{ id: activeDragPage.id }} />
                  )}
                </Box>
              </div>
            ) : null}
          </DragOverlay>,
          document.body,
        )}
      </DndContext>
    </SpaceBetween>
  );

  const secondaryReason = secondarySave?.disabledReason?.(sortSectionsByFirstPage(draft));
  const footer = (
    <Box float="right">
      <SpaceBetween direction="horizontal" size="xs">
        <Button variant="link" onClick={onCancel} disabled={isSaving}>
          <FormattedMessage {...messages.cancel} />
        </Button>
        {secondarySave && (
          <Button
            disabled={isSaving || !validation.isValid || !isChanged || !!secondaryReason}
            disabledReason={secondaryReason}
            onClick={() => void secondarySave.onSave(sortSectionsByFirstPage(draft))}
          >
            {secondarySave.label}
          </Button>
        )}
        <Button
          variant="primary"
          loading={isSaving}
          disabled={!validation.isValid || !isChanged}
          onClick={() => onSave(sortSectionsByFirstPage(draft))}
        >
          {saveLabel ?? <FormattedMessage {...messages.defaultSave} />}
        </Button>
      </SpaceBetween>
    </Box>
  );

  /**
   * A page at readable size, because the thumbnail is not enough to decide grouping.
   *
   * Rendered as a **sibling** of the expanded board rather than inside it: two Cloudscape
   * modals stack by z-index, whereas nesting one inside another is not something the
   * library promises to handle. So the same element serves the inline and expanded
   * layouts, and the preview works from either.
   *
   * `size="max"` and an unconstrained image: the point is to see detail, and the source is
   * already ~10x the 112px the card shows, so there is nothing to fetch and no reason to
   * shrink it.
   */
  const previewPage = previewPageId === null ? undefined : pageById.get(previewPageId);
  const preview = previewPage && (
    <Modal
      visible
      size="max"
      header={intl.formatMessage(messages.pageLabel, { id: previewPage.id })}
      onDismiss={() => setPreviewPageId(null)}
      footer={
        <Box float="right">
          <Button onClick={() => setPreviewPageId(null)}>
            <FormattedMessage {...messages.close} />
          </Button>
        </Box>
      }
    >
      {previewPage.imageUri ? (
        <img
          src={previewPage.imageUri}
          alt={intl.formatMessage(messages.pageLabel, { id: previewPage.id })}
          style={{ width: '100%', height: 'auto', display: 'block' }}
        />
      ) : (
        <Box textAlign="center" padding="xl">
          <Spinner /> <FormattedMessage {...messages.notRendered} />
        </Box>
      )}
    </Modal>
  );

  // Same body either way, so expanding cannot drift from the inline view.
  if (isExpanded) {
    return (
      <>
        <Modal
          visible
          width={MODAL_WIDTH}
          header={title ?? intl.formatMessage(messages.defaultTitle)}
          onDismiss={asModal ? onCancel : () => setIsExpanded(false)}
          footer={footer}
        >
          {/* Wide enough for three section columns side by side; more scroll sideways. */}
          <div style={{ minWidth: `${MODAL_MIN_WIDTH}px` }}>{body}</div>
        </Modal>
        {preview}
      </>
    );
  }

  return (
    <SpaceBetween size="m">
      {body}
      {footer}
      {preview}
    </SpaceBetween>
  );
};

export default PageGroupingEditor;
