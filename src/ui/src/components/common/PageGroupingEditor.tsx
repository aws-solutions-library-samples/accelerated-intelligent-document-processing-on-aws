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
  onCancel: () => void;
}

/** Column page-strip height. Taller in the modal, which is the point of expanding. */
const INLINE_COLUMN_HEIGHT = 320;
const EXPANDED_COLUMN_HEIGHT = 620;

const nextSectionId = (sections: GroupedSection[]): string => {
  const numeric = sections.map((s) => Number.parseInt(s.sectionId, 10)).filter((n) => Number.isFinite(n));
  return String((numeric.length > 0 ? Math.max(...numeric) : 0) + 1);
};

const CARD_WIDTH = 112;

/** The thumbnail itself, with no drag wiring — shared by the card and the drag overlay. */
const PageThumb = ({ page }: { page: GroupingPage }): React.JSX.Element =>
  page.imageUri ? (
    <img src={page.imageUri} alt={`Page ${page.id}`} style={{ width: '100%', display: 'block', borderRadius: '2px' }} />
  ) : (
    <Box textAlign="center" padding="s">
      <Spinner />
    </Box>
  );

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
  const moving = movesWithSelection
    ? `page ${page.id} and ${selectionSize - 1} other selected page${selectionSize > 2 ? 's' : ''}`
    : `page ${page.id}`;

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
            ? `Page ${page.id}, drag to move it and ${selectionSize - 1} other selected page${selectionSize > 2 ? 's' : ''}`
            : `Page ${page.id}, drag to reorder it or move it to another section`
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
          <Checkbox checked={isSelected} onChange={() => onToggleSelect(page.id, false)} ariaLabel={`Select page ${page.id}`}>
            {page.id}
          </Checkbox>
        </span>
        <ButtonDropdown
          variant="icon"
          ariaLabel={`Move ${moving}`}
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
            { id: '__view', text: 'View full page' },
            {
              id: '__reorder',
              text: 'Order within this section',
              items: [
                { id: '__earlier', text: 'Move earlier', disabled: isFirstInSection },
                { id: '__later', text: 'Move later', disabled: isLastInSection },
              ],
            },
            ...(otherSections.length > 0
              ? [
                  {
                    id: '__move',
                    text: 'Move to section',
                    items: otherSections.map((s) => ({
                      id: s.sectionId,
                      text: `Section ${s.sectionId}${s.documentClass ? ` (${s.documentClass})` : ''}`,
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
  errors: string[];
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
                    ariaLabel={`Sort section ${section.sectionId} pages in document order`}
                    onClick={() => onSortPages(section.sectionId)}
                  />
                )}
                <Button
                  iconName="remove"
                  variant="icon"
                  ariaLabel={`Delete section ${section.sectionId}`}
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
            Section {section.sectionId}
          </Header>
        }
      >
        <SpaceBetween size="s">
          {classOptions.length > 0 && (
            <Select
              selectedOption={selected ?? (section.documentClass ? { label: section.documentClass, value: section.documentClass } : null)}
              options={classOptions}
              disabled={!canChangeClass}
              placeholder="Choose a document class"
              onChange={({ detail }: { detail: SelectProps.ChangeDetail }) =>
                onClassChange(section.sectionId, detail.selectedOption.value ?? '')
              }
            />
          )}

          {customOrder && <Badge color="blue">Custom page order</Badge>}

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
                Drop a page here.
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
                {errors.map((e) => (
                  <span key={e}>{e}</span>
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
  saveLabel = 'Save grouping',
  isSaving = false,
  onSave,
  onCancel,
}: PageGroupingEditorProps): React.JSX.Element => {
  // Sorted once, on open. NOT re-sorted as the draft changes — see the stable-order note
  // at the top of the file.
  const [draft, setDraft] = useState<GroupedSection[]>(() => sortSectionsByFirstPage(sections));
  const [selectedPageIds, setSelectedPageIds] = useState<Set<number>>(new Set());
  const [lastTouchedPageId, setLastTouchedPageId] = useState<number | null>(null);
  const [isExpanded, setIsExpanded] = useState(false);
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

  const addSection = () => setDraft((prev) => [...prev, { sectionId: nextSectionId(prev), documentClass: null, pageIds: [] }]);
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
        <Alert type="error" header="This grouping is not valid yet">
          <SpaceBetween size="xxs">
            {validation.document.map((e) => (
              <span key={e}>{e}</span>
            ))}
          </SpaceBetween>
        </Alert>
      )}

      <SpaceBetween direction="horizontal" size="xs" alignItems="center">
        <Badge color={validation.isValid ? 'green' : 'red'}>
          {pages.length} page{pages.length === 1 ? '' : 's'} · {draft.length} section{draft.length === 1 ? '' : 's'}
        </Badge>
        {selectedPageIds.size > 0 && (
          <>
            <Box fontSize="body-s" color="text-status-info">
              {selectedPageIds.size} page{selectedPageIds.size === 1 ? '' : 's'} selected — moving any one moves all of them
            </Box>
            <Button variant="link" onClick={() => setSelectedPageIds(new Set())}>
              Clear selection
            </Button>
          </>
        )}
        <Button iconName="add-plus" onClick={addSection}>
          Add section
        </Button>
        {!isExpanded && (
          <Button iconName="expand" onClick={() => setIsExpanded(true)}>
            Expand
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
                  {draggingCount > 1 ? `${draggingCount} pages` : `Page ${activeDragPage.id}`}
                </Box>
              </div>
            ) : null}
          </DragOverlay>,
          document.body,
        )}
      </DndContext>
    </SpaceBetween>
  );

  const footer = (
    <Box float="right">
      <SpaceBetween direction="horizontal" size="xs">
        <Button variant="link" onClick={onCancel} disabled={isSaving}>
          Cancel
        </Button>
        <Button
          variant="primary"
          loading={isSaving}
          disabled={!validation.isValid || !isChanged}
          onClick={() => onSave(sortSectionsByFirstPage(draft))}
        >
          {saveLabel}
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
      header={`Page ${previewPage.id}`}
      onDismiss={() => setPreviewPageId(null)}
      footer={
        <Box float="right">
          <Button onClick={() => setPreviewPageId(null)}>Close</Button>
        </Box>
      }
    >
      {previewPage.imageUri ? (
        <img src={previewPage.imageUri} alt={`Page ${previewPage.id}`} style={{ width: '100%', height: 'auto', display: 'block' }} />
      ) : (
        <Box textAlign="center" padding="xl">
          <Spinner /> This page has not finished rendering.
        </Box>
      )}
    </Modal>
  );

  // Same body either way, so expanding cannot drift from the inline view.
  if (isExpanded) {
    return (
      <>
        <Modal visible size="max" header="Edit page grouping" onDismiss={() => setIsExpanded(false)} footer={footer}>
          {body}
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
