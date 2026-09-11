// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The workspace's per-set state must not survive a change of set.
 *
 * Reproduced on a live stack: Start annotating on set A, then navigate in-app to set
 * B's queue. B rendered with A's `v1 → v2` badge, a `?v=2` queue link, no Start
 * annotating prompt and an unlocked editor — while B had no draft and no snapshot in
 * S3. That is precisely the silent commitment the transition exists to prevent, on the
 * ordinary path of an annotator working two assigned sets in one session. For a few
 * seconds B's heading also sat above A's documents.
 *
 * The cause was `openedDraft` (and the queue) living in component state that nothing
 * keyed on `testSetId` ever reset. Rather than enumerate every piece of per-set state
 * in a reset effect — and miss the next one added — the route remounts the component
 * per set, which drops all of it at once.
 *
 * Asserted at source level, following the sibling tests in this directory.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

const HERE = join(__dirname, '..');
const ROUTES = readFileSync(join(HERE, '..', '..', 'routes', 'TestStudioRoutes.tsx'), 'utf-8');
const SOURCE = readFileSync(join(HERE, 'AnnotationWorkspace.tsx'), 'utf-8');

describe('one workspace instance per test set', () => {
  it('remounts the workspace when the set in the URL changes', () => {
    expect(ROUTES).toMatch(/<AnnotationWorkspace key=\{testSetId\} \/>/);
  });

  it('and the annotate route renders that keyed wrapper, not the bare component', () => {
    // A wrapper that exists but is not routed to would pass the test above and fix
    // nothing.
    const annotateRoute = ROUTES.slice(ROUTES.indexOf('path="sets/:testSetId/annotate"'), ROUTES.indexOf('path="annotate"'));
    expect(annotateRoute).toMatch(/<AnnotationWorkspaceForSet \/>/);
    expect(annotateRoute).not.toMatch(/<AnnotationWorkspace \/>/);
  });
});

describe('what the reviewer sees when a transition opens', () => {
  it('confirms the commitment with what the server reports was preserved', () => {
    // The alert used to vanish and a small badge appear, and the server's
    // snapshotObjectCount went unread. Now the moment it happens is said out loud.
    expect(SOURCE).toMatch(
      /Version \$\{opened\.baseVersion\} preserved \(\$\{\(opened\.snapshotObjectCount \?\? 0\)\.toLocaleString\(\)\} objects\)/,
    );
    expect(SOURCE).toMatch(/id: 'annotation-draft-opened'/);
  });

  it('says so differently when the transition was already open', () => {
    expect(SOURCE).toMatch(/opened\.alreadyOpen\s*\?\s*`Version \$\{opened\.draftVersion\} was already open/);
  });

  it('explains the badge on hover', () => {
    expect(SOURCE).toMatch(/Corrections made here go into version \$\{draft\.draftVersion\}/);
  });
});

describe('the authored-ground-truth alert agrees with the rest of the screen', () => {
  it('does not promise editing before a transition is open', () => {
    // The editor is read-only until then, and this alert stacked directly under the
    // one saying so.
    expect(SOURCE).toMatch(/canSaveDirectToBaseline\s*\?\s*draft\s*\?\s*' You can still correct it below/);
    expect(SOURCE).toMatch(/' Use Start annotating, above, to correct it\.'/);
  });

  it('no longer claims the direct save records no review', () => {
    // The direct save writes reviewed-human and counts toward progress since the
    // provenance fix; the old copy contradicted it.
    expect(SOURCE).not.toMatch(/rather than recording a review/);
    expect(SOURCE).toMatch(/counts the document as reviewed/);
  });
});

describe('one route to reviewed documents at a time', () => {
  it('offers Show reviewed documents once in the rail, not again inside the empty state', () => {
    // Both showed at once when a search matched nothing. The rail link and the
    // Queue complete alert's action are the two that remain.
    const occurrences = SOURCE.split('setShowReviewed(true)').length - 1;
    expect(occurrences).toBe(2);
  });
});
