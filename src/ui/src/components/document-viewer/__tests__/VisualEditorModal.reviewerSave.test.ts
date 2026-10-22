// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * A Reviewer's Save must not complete the review.
 *
 * Reviewers cannot call `uploadDocument`, so their Save goes through a server-side
 * mutation. It used `completeSectionReview`, which also marks the section reviewed:
 * on a single-section document, or the last pending section, one Save set the whole
 * document to Review Completed, started reprocessing and locked the Reviewer out of
 * Edit Mode. The reviewer path now calls `saveSectionReviewEdits`, which writes the
 * edits and leaves the review In Progress; Process Changes or Skip completes it.
 *
 * Asserted against the source, as VisualEditorModal.classGating.test.ts does, because
 * the decision is which mutation one branch names.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

const SOURCE = readFileSync(join(__dirname, '..', 'VisualEditorModal.tsx'), 'utf-8');

/** The body of the `if (isReviewerOnly) { ... } else {` branch in the save handler. */
const reviewerBranch = (): string => {
  const start = SOURCE.indexOf('if (isReviewerOnly) {');
  expect(start, 'reviewer save branch not found').toBeGreaterThan(-1);
  const end = SOURCE.indexOf('} else {', start);
  return SOURCE.slice(start, end);
};

describe('VisualEditorModal reviewer save', () => {
  it('saves reviewer edits with the save-only mutation', () => {
    expect(reviewerBranch()).toMatch(/query:\s*saveSectionReviewEdits\b/);
  });

  it('never completes the section review from Save', () => {
    expect(SOURCE).not.toMatch(/\bcompleteSectionReview\b/);
  });

  it('keeps Admin and Author on the presigned upload path', () => {
    const elseBranch = SOURCE.slice(SOURCE.indexOf('} else {', SOURCE.indexOf('if (isReviewerOnly) {')));
    expect(elseBranch).toMatch(/query:\s*uploadDocument\b/);
  });
});
