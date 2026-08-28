// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Which pages belong to which section of a packet, and whether a grouping is legal.
 *
 * A packet split is a **partition**: every page of the document belongs to exactly one
 * section. That is the property the drag-and-drop editor has to preserve, and it is the
 * property `split_document.page_indices` encodes as ground truth — see
 * `stickler_backend/doc_split.py:load_sections_for_doc_split`, which scores
 * classification against it.
 *
 * Extracted from SectionsPanel's inline `validateSections`, which had the same rules
 * minus the orphan check, so both the test-set annotate view and the document view can
 * share one definition of "valid grouping" rather than drifting apart.
 *
 * ## This module is deliberately base-agnostic
 *
 * It never assumes page numbers start at 0 or 1. It only requires that every page a
 * section claims is drawn from the `availablePageIds` the caller supplies. That matters
 * because the two surfaces genuinely differ:
 *
 * - Test-set baselines store `split_document.page_indices`, **0-based**.
 * - `TestDocPage.Id` from `use-test-doc-pages` is **1-based**.
 * - Document-view `PageIds` are 1-based, except BDA / Pattern-1, which is 0-based —
 *   which is why SectionsPanel's own check allows `pageId >= 0` rather than `> 0`.
 *
 * Baking a base in here would put an off-by-one between two surfaces into shared code,
 * where it would silently shift every grouping by a page. Conversion belongs in the save
 * adapter that knows which space it is in; see `pageIndicesToIds` / `pageIdsToIndices`
 * below, which are the *only* arithmetic in the feature and are tested both ways.
 */

/** A section and the pages assigned to it, in the caller's own numbering. */
export interface GroupedSection {
  sectionId: string;
  /** The class this section is labelled as. Absent for a section not yet classified. */
  documentClass?: string | null;
  pageIds: number[];
}

export interface GroupingValidation {
  /** Messages that belong to one section, keyed by its id. */
  bySection: Record<string, string[]>;
  /**
   * Messages about the grouping as a whole — an orphaned page belongs to no section by
   * definition, so it cannot be reported under one.
   */
  document: string[];
  isValid: boolean;
}

/**
 * Validate a proposed grouping.
 *
 * Reports everything it finds rather than stopping at the first problem: a reviewer
 * dragging pages around wants to see all of what is left to fix, not one error at a
 * time.
 */
export const validateGrouping = (sections: GroupedSection[], availablePageIds: number[]): GroupingValidation => {
  const bySection: Record<string, string[]> = {};
  const documentErrors: string[] = [];
  const claimedBy = new Map<number, string>();
  const seenSectionIds = new Set<string>();
  const available = new Set(availablePageIds);

  const push = (sectionId: string, message: string) => {
    bySection[sectionId] = [...(bySection[sectionId] ?? []), message];
  };

  sections.forEach((section) => {
    const id = section.sectionId;

    if (!id || !id.trim()) {
      documentErrors.push('A section has no id.');
    } else if (seenSectionIds.has(id)) {
      push(id, `Section id '${id}' is used by more than one section.`);
    } else {
      seenSectionIds.add(id);
    }

    if (section.pageIds.length === 0) {
      // Blocked rather than auto-removed: deleting a section discards its field values,
      // so it should always be a deliberate act.
      push(id, 'This section has no pages. Move a page into it, or delete the section.');
      return;
    }

    const notInDocument: number[] = [];
    section.pageIds.forEach((pageId) => {
      if (!Number.isInteger(pageId)) {
        notInDocument.push(pageId);
      } else if (!available.has(pageId)) {
        notInDocument.push(pageId);
      } else if (claimedBy.has(pageId)) {
        push(id, `Page ${pageId} is already in section ${claimedBy.get(pageId)}.`);
      } else {
        claimedBy.set(pageId, id);
      }
    });

    if (notInDocument.length > 0) {
      push(id, `Page${notInDocument.length > 1 ? 's' : ''} ${notInDocument.join(', ')} not in this document.`);
    }
  });

  // The rule SectionsPanel lacked. A page in no section is not merely untidy: the
  // document's ground truth would claim the page does not exist, and doc-split scoring
  // would mark it unmatched.
  const orphans = availablePageIds.filter((pageId) => !claimedBy.has(pageId));
  if (orphans.length > 0) {
    documentErrors.push(
      `Page${orphans.length > 1 ? 's' : ''} ${orphans.join(', ')} ${orphans.length > 1 ? 'are' : 'is'} not in any section. Every page has to belong to one.`,
    );
  }

  return {
    bySection,
    document: documentErrors,
    isValid: Object.keys(bySection).length === 0 && documentErrors.length === 0,
  };
};

/**
 * `split_document.page_indices` (0-based) → `TestDocPage.Id` (1-based).
 *
 * The whole feature's arithmetic is these two functions. Both are tested in both
 * directions, including the round trip, because an off-by-one here would rewrite every
 * baseline it touched without erroring.
 */
export const pageIndicesToIds = (pageIndices: number[]): number[] => pageIndices.map((index) => index + 1);

/**
 * `TestDocPage.Id` (1-based) → `split_document.page_indices` (0-based).
 *
 * Sorted ascending: `page_indices` describes which pages a section contains, not the
 * order they were dropped in, and downstream readers assume document order.
 */
export const pageIdsToIndices = (pageIds: number[]): number[] => pageIds.map((id) => id - 1).sort((a, b) => a - b);

/**
 * Sections in document order, by their first page.
 *
 * Doc-split scoring reports `order_matched` alongside the class match, so a grouping
 * whose sections run out of page order scores worse for a reason that has nothing to do
 * with the reviewer's intent. Same ordering rule as SectionsPanel's
 * `sortSectionsByPageId`.
 */
export const sortSectionsByFirstPage = <T extends { pageIds: number[] }>(sections: T[]): T[] =>
  [...sections].sort((a, b) => {
    const firstA = a.pageIds.length > 0 ? Math.min(...a.pageIds) : Number.POSITIVE_INFINITY;
    const firstB = b.pageIds.length > 0 ? Math.min(...b.pageIds) : Number.POSITIVE_INFINITY;
    return firstA - firstB;
  });
