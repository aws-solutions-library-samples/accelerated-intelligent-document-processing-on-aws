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
 * ## Two simplifications, both earned rather than assumed
 *
 * **Draggable/droppable, not sortable.** `pageIdsToIndices` normalises to ascending
 * document order, so a page's position *within* a section carries no meaning. Sorting
 * machinery would imply otherwise and bring the edge cases that come with it. Pages
 * therefore always display in document order, whatever order they were dropped in.
 *
 * **Dragging is not the only way.** Every page also carries a "Move to" menu. A
 * drag-only interface is unusable with a keyboard or a screen reader in practice — even
 * with dnd-kit's KeyboardSensor, which is wired up here — and this is the one screen an
 * annotator cannot route around. Same lesson as the locate button in FormFieldRenderer:
 * a mouse gesture must never be the sole path to a capability.
 */

import React, { useMemo, useState } from 'react';
import {
  DndContext,
  DragEndEvent,
  KeyboardSensor,
  PointerSensor,
  closestCenter,
  useDraggable,
  useDroppable,
  useSensor,
  useSensors,
} from '@dnd-kit/core';
import { Alert, Badge, Box, Button, ButtonDropdown, Container, Header, Select, SpaceBetween, Spinner } from '@cloudscape-design/components';
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

const nextSectionId = (sections: GroupedSection[]): string => {
  const numeric = sections.map((s) => Number.parseInt(s.sectionId, 10)).filter((n) => Number.isFinite(n));
  return String((numeric.length > 0 ? Math.max(...numeric) : 0) + 1);
};

/** A page thumbnail that can be dragged, and moved without dragging. */
const PageCard = ({
  page,
  sectionId,
  otherSections,
  onMove,
}: {
  page: GroupingPage;
  sectionId: string;
  otherSections: GroupedSection[];
  onMove: (pageId: number, toSectionId: string) => void;
}): React.JSX.Element => {
  const { attributes, listeners, setNodeRef, transform, isDragging } = useDraggable({
    id: `page-${page.id}`,
    data: { pageId: page.id, fromSectionId: sectionId },
  });

  return (
    <div
      ref={setNodeRef}
      style={{
        transform: transform ? `translate3d(${transform.x}px, ${transform.y}px, 0)` : undefined,
        opacity: isDragging ? 0.4 : 1,
        border: '1px solid #d5dbdb',
        borderRadius: '4px',
        padding: '4px',
        background: '#ffffff',
        width: '112px',
      }}
    >
      <div {...attributes} {...listeners} style={{ cursor: 'grab' }} aria-label={`Page ${page.id}, drag to another section`}>
        {page.imageUri ? (
          <img src={page.imageUri} alt={`Page ${page.id}`} style={{ width: '100%', display: 'block', borderRadius: '2px' }} />
        ) : (
          <Box textAlign="center" padding="s">
            <Spinner />
          </Box>
        )}
      </div>
      <SpaceBetween direction="horizontal" size="xxs" alignItems="center">
        <Box fontSize="body-s">Page {page.id}</Box>
        {/* The keyboard and screen-reader path. Not a secondary nicety: dragging is
            unusable without a pointer, and this screen is not optional for an
            annotator. */}
        {otherSections.length > 0 && (
          <ButtonDropdown
            variant="icon"
            ariaLabel={`Move page ${page.id} to another section`}
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

/** One section's pages, and a drop target for pages arriving from elsewhere. */
const SectionColumn = ({
  section,
  pages,
  errors,
  classOptions,
  canChangeClass,
  otherSections,
  onClassChange,
  onDelete,
  onMove,
}: {
  section: GroupedSection;
  pages: GroupingPage[];
  errors: string[];
  classOptions: ConfigClassOption[];
  canChangeClass: boolean;
  otherSections: GroupedSection[];
  onClassChange: (sectionId: string, value: string) => void;
  onDelete: (sectionId: string) => void;
  onMove: (pageId: number, toSectionId: string) => void;
}): React.JSX.Element => {
  const { setNodeRef, isOver } = useDroppable({ id: `section-${section.sectionId}`, data: { sectionId: section.sectionId } });
  const selected = classOptions.find((o) => o.value === section.documentClass);

  return (
    <Container
      header={
        <Header
          variant="h3"
          actions={
            <Button
              iconName="remove"
              variant="icon"
              ariaLabel={`Delete section ${section.sectionId}`}
              /* Only an empty section can go: deleting one with pages would orphan them,
                 and deleting one with field values should be deliberate. Drag the pages
                 out first — validateGrouping's empty-section error says exactly that. */
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
            gap: '8px',
            minHeight: '96px',
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
              <PageCard key={page.id} page={page} sectionId={section.sectionId} otherSections={otherSections} onMove={onMove} />
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
  const [draft, setDraft] = useState<GroupedSection[]>(() => sortSectionsByFirstPage(sections));
  const sensors = useSensors(useSensor(PointerSensor), useSensor(KeyboardSensor));

  const pageById = useMemo(() => new Map(pages.map((p) => [p.id, p])), [pages]);
  const availablePageIds = useMemo(() => pages.map((p) => p.id), [pages]);
  const validation = useMemo(() => validateGrouping(draft, availablePageIds), [draft, availablePageIds]);
  const isChanged = useMemo(() => JSON.stringify(sortSectionsByFirstPage(sections)) !== JSON.stringify(draft), [sections, draft]);
  const ordered = useMemo(() => sortSectionsByFirstPage(draft), [draft]);

  const movePage = (pageId: number, toSectionId: string) => {
    setDraft((prev) =>
      prev.map((section) => {
        if (section.sectionId === toSectionId) {
          // Ascending, because page_indices records membership rather than drop order.
          return section.pageIds.includes(pageId) ? section : { ...section, pageIds: [...section.pageIds, pageId].sort((a, b) => a - b) };
        }
        return { ...section, pageIds: section.pageIds.filter((id) => id !== pageId) };
      }),
    );
  };

  const handleDragEnd = ({ active, over }: DragEndEvent) => {
    if (!over) return;
    const pageId = (active.data.current as { pageId?: number } | undefined)?.pageId;
    const toSectionId = (over.data.current as { sectionId?: string } | undefined)?.sectionId;
    if (pageId === undefined || !toSectionId) return;
    movePage(pageId, toSectionId);
  };

  const addSection = () => setDraft((prev) => [...prev, { sectionId: nextSectionId(prev), documentClass: null, pageIds: [] }]);
  const deleteSection = (sectionId: string) => setDraft((prev) => prev.filter((s) => s.sectionId !== sectionId));
  const changeClass = (sectionId: string, value: string) =>
    setDraft((prev) => prev.map((s) => (s.sectionId === sectionId ? { ...s, documentClass: value } : s)));

  return (
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
        <Button iconName="add-plus" onClick={addSection}>
          Add section
        </Button>
      </SpaceBetween>

      <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={handleDragEnd}>
        <SpaceBetween size="s">
          {ordered.map((section) => (
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
              onClassChange={changeClass}
              onDelete={deleteSection}
              onMove={movePage}
            />
          ))}
        </SpaceBetween>
      </DndContext>

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
    </SpaceBetween>
  );
};

export default PageGroupingEditor;
