// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The rules a packet split has to obey, and the one piece of arithmetic in the feature.
 *
 * The conversion tests come first and are the most important in the feature: an
 * off-by-one between `split_document.page_indices` (0-based) and `TestDocPage.Id`
 * (1-based) would rewrite every baseline it touched, shifting each section by a page,
 * without raising anything. Nothing downstream would catch it — the file would still be
 * valid JSON with plausible numbers.
 */

import { describe, expect, it } from 'vitest';

import { pageIdsToIndices, pageIndicesToIds, sortSectionsByFirstPage, validateGrouping } from '../section-grouping';

describe('page numbering conversion', () => {
  it('converts 0-based baseline indices to 1-based page ids', () => {
    // A 3-page section starting at the first page of the document.
    expect(pageIndicesToIds([0, 1, 2])).toEqual([1, 2, 3]);
    // And one that does not start at the beginning.
    expect(pageIndicesToIds([3, 4])).toEqual([4, 5]);
  });

  it('converts 1-based page ids back to 0-based baseline indices', () => {
    expect(pageIdsToIndices([1, 2, 3])).toEqual([0, 1, 2]);
    expect(pageIdsToIndices([4, 5])).toEqual([3, 4]);
  });

  it('round-trips without drift, which is the property that matters', () => {
    for (const indices of [[0], [0, 1], [2, 5, 9], [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]]) {
      expect(pageIdsToIndices(pageIndicesToIds(indices))).toEqual(indices);
    }
  });

  it('sorts indices ascending regardless of the order pages were dropped', () => {
    // page_indices says which pages a section contains, not the order they were dragged.
    expect(pageIdsToIndices([5, 1, 3])).toEqual([0, 2, 4]);
  });

  it('never produces a negative index from a valid 1-based id', () => {
    // Page 1 is the first page; index 0 is its baseline form. A 0 arriving here would
    // mean the caller was already in index space and converted twice.
    expect(pageIdsToIndices([1])).toEqual([0]);
    expect(Math.min(...pageIdsToIndices([1, 2, 3]))).toBe(0);
  });
});

describe('validateGrouping', () => {
  const PAGES = [1, 2, 3, 4, 5];

  it('accepts a partition of every page', () => {
    const result = validateGrouping(
      [
        { sectionId: '1', pageIds: [1, 2] },
        { sectionId: '2', pageIds: [3] },
        { sectionId: '3', pageIds: [4, 5] },
      ],
      PAGES,
    );

    expect(result.isValid).toBe(true);
    expect(result.bySection).toEqual({});
    expect(result.document).toEqual([]);
  });

  it('accepts a non-contiguous section, which the pipeline can genuinely produce', () => {
    const result = validateGrouping(
      [
        { sectionId: '1', pageIds: [1, 3, 5] },
        { sectionId: '2', pageIds: [2, 4] },
      ],
      PAGES,
    );

    expect(result.isValid).toBe(true);
  });

  it('rejects an orphaned page — the rule SectionsPanel was missing', () => {
    // Page 5 in no section would claim, as ground truth, that page 5 is not part of the
    // document. doc-split scoring would report it unmatched.
    const result = validateGrouping([{ sectionId: '1', pageIds: [1, 2, 3, 4] }], PAGES);

    expect(result.isValid).toBe(false);
    expect(result.document.join(' ')).toMatch(/Page 5 is not in any section/);
  });

  it('names every orphan, not just the first', () => {
    const result = validateGrouping([{ sectionId: '1', pageIds: [1] }], PAGES);

    expect(result.document.join(' ')).toMatch(/Pages 2, 3, 4, 5 are not in any section/);
  });

  it('rejects a page claimed by two sections', () => {
    const result = validateGrouping(
      [
        { sectionId: '1', pageIds: [1, 2, 3] },
        { sectionId: '2', pageIds: [3, 4, 5] },
      ],
      PAGES,
    );

    expect(result.isValid).toBe(false);
    expect(result.bySection['2'].join(' ')).toMatch(/Page 3 is already in section 1/);
  });

  it('blocks an empty section rather than removing it silently', () => {
    // Deleting a section discards its field values, so it stays a deliberate act.
    const result = validateGrouping(
      [
        { sectionId: '1', pageIds: [1, 2, 3, 4, 5] },
        { sectionId: '2', pageIds: [] },
      ],
      PAGES,
    );

    expect(result.isValid).toBe(false);
    expect(result.bySection['2'].join(' ')).toMatch(/no pages/);
  });

  it('rejects a page that is not in the document', () => {
    const result = validateGrouping(
      [
        { sectionId: '1', pageIds: [1, 2, 3, 4, 5] },
        { sectionId: '2', pageIds: [99] },
      ],
      PAGES,
    );

    expect(result.isValid).toBe(false);
    expect(result.bySection['2'].join(' ')).toMatch(/Page 99 not in this document/);
  });

  it('rejects duplicate section ids', () => {
    const result = validateGrouping(
      [
        { sectionId: '1', pageIds: [1, 2] },
        { sectionId: '1', pageIds: [3, 4, 5] },
      ],
      PAGES,
    );

    expect(result.isValid).toBe(false);
    expect(result.bySection['1'].join(' ')).toMatch(/used by more than one section/);
  });

  it('reports every problem at once, not one per attempt', () => {
    // A reviewer mid-drag wants the full list of what is left to fix.
    const result = validateGrouping(
      [
        { sectionId: '1', pageIds: [1, 99] },
        { sectionId: '2', pageIds: [] },
      ],
      PAGES,
    );

    expect(result.bySection['1']).toHaveLength(1);
    expect(result.bySection['2']).toHaveLength(1);
    expect(result.document).toHaveLength(1); // pages 2-5 orphaned
  });

  it('is base-agnostic: a 0-based document validates the same way', () => {
    // BDA / Pattern-1 numbers pages from 0. The module must not assume otherwise —
    // baking a base in here is how an off-by-one reaches both surfaces at once.
    const zeroBased = [0, 1, 2];
    const result = validateGrouping(
      [
        { sectionId: '1', pageIds: [0, 1] },
        { sectionId: '2', pageIds: [2] },
      ],
      zeroBased,
    );

    expect(result.isValid).toBe(true);
  });
});

describe('sortSectionsByFirstPage', () => {
  it('orders sections by their first page, because scoring reports order_matched', () => {
    const sorted = sortSectionsByFirstPage([
      { sectionId: 'c', pageIds: [5, 6] },
      { sectionId: 'a', pageIds: [1, 2] },
      { sectionId: 'b', pageIds: [3] },
    ]);

    expect(sorted.map((s) => s.sectionId)).toEqual(['a', 'b', 'c']);
  });

  it('uses the lowest page, not the first listed', () => {
    const sorted = sortSectionsByFirstPage([
      { sectionId: 'later', pageIds: [9, 2] },
      { sectionId: 'earlier', pageIds: [1] },
    ]);

    expect(sorted.map((s) => s.sectionId)).toEqual(['earlier', 'later']);
  });

  it('does not mutate its input', () => {
    const input = [
      { sectionId: 'b', pageIds: [3] },
      { sectionId: 'a', pageIds: [1] },
    ];
    sortSectionsByFirstPage(input);

    expect(input.map((s) => s.sectionId)).toEqual(['b', 'a']);
  });

  it('puts an empty section last rather than first', () => {
    // Math.min of [] is Infinity, not -Infinity; getting this backwards would sort a
    // half-finished section to the top of the board mid-drag.
    const sorted = sortSectionsByFirstPage([
      { sectionId: 'empty', pageIds: [] },
      { sectionId: 'real', pageIds: [1] },
    ]);

    expect(sorted.map((s) => s.sectionId)).toEqual(['real', 'empty']);
  });
});
