// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The Test Studio queue table's one sortable column, Annotator. The table opens
// worst-first (most confidence alerts first); sorting by Annotator groups each person's
// documents, keeping that worst-first order within each group.

/** The column's sorting field: who each document is assigned to. */
export const ANNOTATOR_SORTING_COLUMN = { sortingField: 'assignedTo' };

/**
 * Documents by assignee (A to Z, or Z to A), keeping their order among one assignee's
 * documents. Unassigned documents come last either way.
 */
export const sortByAnnotator = <T extends { assignedTo?: string | null }>(docs: readonly T[], descending: boolean): T[] =>
  docs
    .map((d, i) => ({ d, i }))
    .sort((a, b) => {
      const x = a.d.assignedTo ?? '';
      const y = b.d.assignedTo ?? '';
      if (!x || !y) return x === y ? a.i - b.i : x ? -1 : 1;
      const by = x.localeCompare(y);
      return (descending ? -by : by) || a.i - b.i;
    })
    .map(({ d }) => d);
