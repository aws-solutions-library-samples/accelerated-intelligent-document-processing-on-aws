// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Re-group a packet's pages into the right sections, by dragging them.
 *
 * Built for the case that blocked the Exxon POC: a document whose packet split put pages
 * in the wrong sections, on a test set carrying annotations that must not be lost. The
 * grouping is ground truth — `split_document.page_indices` is what doc-split scoring
 * reads — so this is an annotation editor, not a layout tool.
 *
 * Persistence-free by design: the caller supplies `onSave`, because the two surfaces
 * write to different places (a test-set baseline vs a processed document's record) while
 * needing identical rules and identical UI. Validation lives in `section-grouping.ts`,
 * shared with the same two surfaces.
 *
 * ## Six decisions, each with a reason
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
 * **Draggable/droppable, not sortable.** `pageIdsToIndices` normalises to ascending
 * document order, so a page's position *within* a section carries no meaning. Sorting
 * machinery would imply otherwise and bring its edge cases along. Pages therefore render
 * in document order however they were dropped.
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
import { GroupedSection, sortSectionsByFirstPage, validateGrouping } from './section-grouping';

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

/** A page thumbnail: selectable, draggable, and movable without dragging. */
const PageCard = ({
  page,
  sectionId,
  isSelected,
  selectionSize,
  otherSections,
  onToggleSelect,
  onMove,
}: {
  page: GroupingPage;
  sectionId: string;
  isSelected: boolean;
  selectionSize: number;
  otherSections: GroupedSection[];
  onToggleSelect: (pageId: number, viaShift: boolean) => void;
  onMove: (pageId: number, toSectionId: string) => void;
}): React.JSX.Element => {
  const { attributes, listeners, setNodeRef, isDragging } = useDraggable({
    id: `page-${page.id}`,
    data: { pageId: page.id, fromSectionId: sectionId },
  });

  // Dragging one page of a selection carries the whole selection, so say so.
  const movesWithSelection = isSelected && selectionSize > 1;

  return (
    <div
      ref={setNodeRef}
      style={{
        /* No transform here on purpose: DragOverlay carries the page while it is in
           flight, because a transformed child cannot escape this column's own
           `overflowY: auto`. See the note at the top of the file. The card stays put
           and dims, marking where the page came from. */
        opacity: isDragging ? 0.4 : 1,
        border: `2px solid ${isSelected ? '#0073bb' : '#d5dbdb'}`,
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
            : `Page ${page.id}, drag to another section`
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
        {otherSections.length > 0 && (
          <ButtonDropdown
            variant="icon"
            ariaLabel={
              movesWithSelection
                ? `Move page ${page.id} and ${selectionSize - 1} other selected page${selectionSize > 2 ? 's' : ''} to another section`
                : `Move page ${page.id} to another section`
            }
            items={otherSections.map((s) => ({
              id: s.sectionId,
              text: `Section ${s.sectionId}${s.documentClass ? ` (${s.documentClass})` : ''}`,
            }))}
            onItemClick={({ detail }) => onMove(page.id, detail.id)}
          />
        )}
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
  onToggleSelect: (pageId: number, viaShift: boolean) => void;
}): React.JSX.Element => {
  const { setNodeRef, isOver } = useDroppable({ id: `section-${section.sectionId}`, data: { sectionId: section.sectionId } });
  const selected = classOptions.find((o) => o.value === section.documentClass);

  return (
    <div style={{ minWidth: '260px', maxWidth: '260px', flex: '0 0 auto' }}>
      <Container
        header={
          <Header
            variant="h3"
            actions={
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
              pages.map((page) => (
                <PageCard
                  key={page.id}
                  page={page}
                  sectionId={section.sectionId}
                  isSelected={selectedPageIds.has(page.id)}
                  selectionSize={selectedPageIds.size}
                  otherSections={otherSections}
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

  /** Move `pageId`, or the whole selection when `pageId` is part of it. */
  const movePages = (pageId: number, toSectionId: string) => {
    const moving = selectedPageIds.has(pageId) ? new Set(selectedPageIds) : new Set([pageId]);
    setDraft((prev) =>
      prev.map((section) => {
        if (section.sectionId === toSectionId) {
          const kept = section.pageIds.filter((id) => !moving.has(id));
          // Ascending, because page_indices records membership rather than drop order.
          return { ...section, pageIds: [...kept, ...moving].sort((a, b) => a - b) };
        }
        return { ...section, pageIds: section.pageIds.filter((id) => !moving.has(id)) };
      }),
    );
    // Clear afterwards: a selection that outlives its move is a trap, because the next
    // drag would silently carry pages the reviewer had forgotten were selected.
    setSelectedPageIds(new Set());
    setLastTouchedPageId(null);
  };

  const handleDragStart = ({ active }: DragStartEvent) =>
    setActivePageId((active.data.current as { pageId?: number } | undefined)?.pageId ?? null);

  const handleDragEnd = ({ active, over }: DragEndEvent) => {
    setActivePageId(null);
    if (!over) return;
    const pageId = (active.data.current as { pageId?: number } | undefined)?.pageId;
    const toSectionId = (over.data.current as { sectionId?: string } | undefined)?.sectionId;
    if (pageId === undefined || !toSectionId) return;
    movePages(pageId, toSectionId);
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
              /* Document order, not drop order — see the note at the top of the file. */
              pages={[...section.pageIds]
                .sort((a, b) => a - b)
                .map((id) => pageById.get(id))
                .filter((p): p is GroupingPage => Boolean(p))}
              errors={validation.bySection[section.sectionId] ?? []}
              classOptions={classOptions}
              canChangeClass={canChangeClass}
              otherSections={draft.filter((s) => s.sectionId !== section.sectionId)}
              selectedPageIds={selectedPageIds}
              columnHeight={columnHeight}
              onClassChange={changeClass}
              onDelete={deleteSection}
              onMove={movePages}
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

  // Same body either way, so expanding cannot drift from the inline view.
  if (isExpanded) {
    return (
      <Modal visible size="max" header="Edit page grouping" onDismiss={() => setIsExpanded(false)} footer={footer}>
        {body}
      </Modal>
    );
  }

  return (
    <SpaceBetween size="m">
      {body}
      {footer}
    </SpaceBetween>
  );
};

export default PageGroupingEditor;
