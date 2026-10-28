// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Sorting a Test Studio queue by Annotator. What has to hold: documents group by
 * assignee, A to Z or Z to A; within one assignee they keep the worst-first order they
 * arrived in; unassigned documents come last both ways; and the table opens worst-first,
 * sorting only when the column is chosen.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

import { sortByAnnotator } from '../annotator-sort';

// Worst-first, as the queue arrives.
const DOCS = [
  { objectKey: 'w1', assignedTo: 'bo@x.com' },
  { objectKey: 'w2', assignedTo: null },
  { objectKey: 'w3', assignedTo: 'al@x.com' },
  { objectKey: 'w4', assignedTo: 'bo@x.com' },
  { objectKey: 'w5', assignedTo: null },
];
const keys = (docs: { objectKey: string }[]) => docs.map((d) => d.objectKey);

describe('sortByAnnotator', () => {
  it('groups by assignee A to Z, keeping worst-first within each, unassigned last', () => {
    expect(keys(sortByAnnotator(DOCS, false))).toEqual(['w3', 'w1', 'w4', 'w2', 'w5']);
  });

  it('reverses the assignees, not the order within them, and keeps unassigned last', () => {
    expect(keys(sortByAnnotator(DOCS, true))).toEqual(['w1', 'w4', 'w3', 'w2', 'w5']);
  });

  it('leaves the input untouched', () => {
    sortByAnnotator(DOCS, false);
    expect(keys(DOCS)).toEqual(['w1', 'w2', 'w3', 'w4', 'w5']);
  });
});

describe('the queue table', () => {
  const SOURCE = readFileSync(join(__dirname, '..', 'AnnotationWorkspace.tsx'), 'utf-8');

  it('opens worst-first and sorts by Annotator only once the column is chosen', () => {
    expect(SOURCE).toMatch(/useState<\{ descending: boolean \} \| null>\(null\)/);
    expect(SOURCE).toMatch(/annotatorSort \? sortByAnnotator\(filteredQueue, annotatorSort\.descending\) : filteredQueue/);
  });

  it('shows who each document is assigned to in a sortable Annotator column', () => {
    expect(SOURCE).toMatch(/id: 'annotator',\s*header: 'Annotator',\s*sortingField: ANNOTATOR_SORTING_COLUMN\.sortingField/);
  });
});
