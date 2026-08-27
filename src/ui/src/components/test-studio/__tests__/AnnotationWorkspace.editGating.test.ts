// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Who may edit ground truth in the annotate view, and which save path is used.
 *
 * Reported as [#674]: an Admin could change a document's class in the document view
 * but not in the annotate view, where the dropdown was disabled reading "You do not
 * have permission to change this class" — two lines under an alert promising they
 * could "correct the values".
 *
 * It was never a permission check. `isReadOnly` was `!canAnnotate ||
 * !selected.reviewObjectKey`, and a document carrying authored ground truth has no
 * review-queue record, so the editor went read-only regardless of role. The
 * constraint text then attributed a queue-state condition to permissions.
 *
 * The save routing is the other half, and the reason the flag could not simply be
 * flipped: `handleSave` calls `completeSectionReview`, which requires
 * `reviewObjectKey` and throws without it. Leaving it wired would have let someone
 * edit and then lose the work on save. So `onSave` is passed only when there is a
 * review to complete; otherwise the editor falls back to its direct-to-S3 write,
 * which is what TestSetDocumentDetail already does and is semantically right here —
 * there is no draft to confirm and no confidence-curve signal to record for a label
 * a human authored.
 *
 * Asserted at source level: rendering AnnotationWorkspace needs the GraphQL client,
 * settings, role hooks, a router and a populated queue, for what is two JSX props.
 * Blunt, but it pins the decision and fails if either half regresses.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

const SOURCE = readFileSync(join(__dirname, '..', 'AnnotationWorkspace.tsx'), 'utf-8');
const DOC_VIEW = readFileSync(join(__dirname, '..', 'TestSetDocumentDetail.tsx'), 'utf-8');

describe('AnnotationWorkspace edit gating', () => {
  it('gates editing on role alone, not on the review record', () => {
    expect(SOURCE).toMatch(/isReadOnly=\{!canAnnotate\}/);
    // The regression: a queue-state condition presented as a permission problem.
    expect(SOURCE).not.toMatch(/isReadOnly=\{!canAnnotate \|\| !selected\.reviewObjectKey\}/);
  });

  it('agrees with the document view, which is where the class WAS editable', () => {
    // Both views must reach the same verdict for the same user on the same document.
    expect(DOC_VIEW).toMatch(/isReadOnly=\{!canWrite\}/);
    expect(SOURCE).toMatch(/isReadOnly=\{!can[A-Za-z]+\}/);
  });

  it('routes through the review API only when there is a review to complete', () => {
    // completeSectionReview requires reviewObjectKey and throws without it, so
    // wiring it unconditionally would lose the edit at save time.
    expect(SOURCE).toMatch(/onSave=\{selected\.reviewObjectKey \? handleSave : undefined\}/);
  });

  it('still refuses to save a review without a review record', () => {
    // The guard inside handleSave stays: it is the backstop if the routing above
    // is ever changed back.
    expect(SOURCE).toMatch(/if \(!selected\?\.reviewObjectKey\) \{\s*throw new Error/);
  });

  it('promises class editing in the alert, now that it is true', () => {
    expect(SOURCE).toMatch(/You can still correct it below, including its class/);
  });
});
