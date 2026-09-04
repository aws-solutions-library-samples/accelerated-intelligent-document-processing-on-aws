// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Sorting contract for the class-confidence column.
 *
 * Unscored rows sort LAST when ascending — the direction that matters, since
 * least-confident-first is how a reviewer finds the pages worth a second look and
 * a block of "—" at the top of that list defeats the click.
 *
 * Descending puts them first: `useCollection` negates the comparator, and no
 * single ordering keeps nulls last both ways. That is pinned here too, so the
 * behaviour is a recorded decision rather than a surprise — and so that anyone
 * "fixing" it by reversing the array sees these tests fail.
 */

import { describe, expect, it } from 'vitest';

import { compareClassConfidence } from '../ClassConfidence';

type Row = { id: string; confidence?: number | null };

const ROWS: Row[] = [
  { id: 'mid', confidence: 0.62 },
  { id: 'unscored-a' },
  { id: 'high', confidence: 0.98 },
  { id: 'low', confidence: 0.41 },
  { id: 'unscored-b', confidence: null },
];

/** Exactly what `useCollection` does: negate the comparator for descending. */
const sortRows = (rows: Row[], isDescending: boolean): string[] =>
  [...rows].sort((a, b) => (isDescending ? -1 : 1) * compareClassConfidence(a.confidence, b.confidence)).map((r) => r.id);

describe('compareClassConfidence', () => {
  it('orders scored rows ascending', () => {
    expect(sortRows(ROWS, false).slice(0, 3)).toEqual(['low', 'mid', 'high']);
  });

  it('orders scored rows descending', () => {
    expect(sortRows(ROWS, true).slice(2)).toEqual(['high', 'mid', 'low']);
  });

  it('keeps unscored rows last ascending', () => {
    expect(sortRows(ROWS, false).slice(3)).toEqual(['unscored-a', 'unscored-b']);
  });

  it('leads with unscored rows descending, by design-system convention', () => {
    // Not the nicer behaviour, but the honest one: useCollection negates the
    // comparator, so nulls cannot stay last in both directions. Pinned so the
    // behaviour is a decision on record, and so that "fixing" it by reversing the
    // sorted array (which breaks the ascending case) fails here.
    expect(sortRows(ROWS, true).slice(0, 2).sort()).toEqual(['unscored-a', 'unscored-b']);
    expect(sortRows(ROWS, true).slice(2)).toEqual(['high', 'mid', 'low']);
  });

  it('treats null and undefined confidence identically', () => {
    expect(compareClassConfidence(null, undefined)).toBe(0);
    expect(compareClassConfidence(undefined, 0.5)).toBeGreaterThan(0);
    expect(compareClassConfidence(0.5, null)).toBeLessThan(0);
  });

  it('treats 0 as a score, not as absent', () => {
    // 0.0 is a real assertion (an errored page); it must sort below every other
    // score rather than being lumped in with "not scored".
    expect(sortRows([{ id: 'zero', confidence: 0 }, { id: 'unscored' }, { id: 'low', confidence: 0.41 }], false)).toEqual([
      'zero',
      'low',
      'unscored',
    ]);
  });
});
