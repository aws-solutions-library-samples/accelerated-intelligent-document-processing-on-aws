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
 * per set, which drops all of it at once. Annotation now lives under HITL
 * (`ReviewQueuesRoutes.tsx`): the set's queue page and each document's annotation
 * screen are separate routes, and both are keyed so a change of set remounts them.
 *
 * The routes are asserted at source level; the copy through the pure helpers in
 * `annotation-edit-flags.ts`.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

import { draftBadgeHover, draftOpenedMessage, groundTruthAlertSuffix } from '../annotation-edit-flags';

const HERE = join(__dirname, '..');
const ROUTES = readFileSync(join(HERE, '..', '..', 'routes', 'ReviewQueuesRoutes.tsx'), 'utf-8');
const TEST_STUDIO_ROUTES = readFileSync(join(HERE, '..', '..', 'routes', 'TestStudioRoutes.tsx'), 'utf-8');
const SCREEN = readFileSync(join(HERE, '..', 'edit-screen', 'TestSetItemEdit.tsx'), 'utf-8');
const ADAPTER = readFileSync(join(HERE, '..', 'edit-screen', 'test-set-item-edit-adapter.ts'), 'utf-8');

/** The body of one `const Name = (...) => { ... };` page component in the routes file. */
const pageBody = (name: string): string => {
  const at = ROUTES.indexOf(`const ${name} = `);
  expect(at, `${name} not found`).toBeGreaterThan(-1);
  return ROUTES.slice(at, ROUTES.indexOf('\n};\n', at));
};
const SOURCE = readFileSync(join(HERE, 'AnnotationWorkspace.tsx'), 'utf-8');

describe('one workspace instance per test set', () => {
  it('remounts the queue page and the edit screen when the set in the URL changes', () => {
    expect(pageBody('TestSetQueuePage')).toMatch(/<AnnotationWorkspace key=\{testSetId\} onName=\{onName\} \/>/);
    expect(pageBody('TestSetItemPage')).toMatch(
      /<TestSetItemEdit\s+key=\{`\$\{testSetId\}\/\$\{objectKey\}`\}\s+testSetId=\{testSetId\}\s+objectKey=\{decodeURIComponent\(objectKey\)\}\s+onTestSet=\{onTestSet\}\s+\/>/,
    );
    // And the screen's own opened transition is dropped on a change of set or document.
    expect(ADAPTER).toMatch(/useEffect\(\(\) => \{\s*setQueue\(null\);\s*setOpenedDraft\(null\);/);
  });

  it('and the routes render those keyed wrappers, not the bare components', () => {
    // A wrapper that exists but is not routed to would pass the test above and fix
    // nothing.
    const route = (path: string) => {
      const at = ROUTES.indexOf(`path="${path}"`);
      expect(at, `route ${path} not found`).toBeGreaterThan(-1);
      return ROUTES.slice(at, ROUTES.indexOf('</RequireRole>', at));
    };
    expect(route('test-sets/:testSetId')).toMatch(/<TestSetQueuePage \/>/);
    expect(route('test-sets/:testSetId/items/:objectKey')).toMatch(/<TestSetItemPage \/>/);
    expect(ROUTES.split('<AnnotationWorkspace ').length - 1).toBe(1);
    // The old Test Studio annotate link only redirects there; it renders no workspace.
    expect(TEST_STUDIO_ROUTES).toMatch(/path="sets\/:testSetId\/annotate" element=\{<AnnotateRedirect \/>\}/);
    expect(TEST_STUDIO_ROUTES).not.toMatch(/<AnnotationWorkspace/);
  });
});

describe('what the reviewer sees when a transition opens', () => {
  it('confirms the commitment with what the server reports was preserved', () => {
    // The alert used to vanish and a small badge appear, and the server's
    // snapshotObjectCount went unread. Now the moment it happens is said out loud.
    expect(draftOpenedMessage({ baseVersion: 1, draftVersion: 2, snapshotObjectCount: 1234 })).toBe(
      `Version 1 preserved (${(1234).toLocaleString()} objects). Your corrections go into version 2.`,
    );
    // Shown through the app's shared notifications, from the screen's Start annotating.
    expect(SCREEN).toMatch(/if \(opened\) setSuccessMessage\(draftOpenedMessage\(opened\)\);/);
  });

  it('says so differently when the transition was already open', () => {
    expect(draftOpenedMessage({ baseVersion: 1, draftVersion: 2, alreadyOpen: true })).toMatch(/^Version 2 was already open/);
  });

  it('explains the badge on hover', () => {
    expect(draftBadgeHover({ baseVersion: 1, draftVersion: 2 })).toMatch(/^Corrections made here go into version 2\./);
  });
});

describe('the authored-ground-truth alert agrees with the rest of the screen', () => {
  it('does not promise editing before a transition is open', () => {
    // The editor is read-only until then, and this alert stacked directly under the
    // one saying so.
    expect(groundTruthAlertSuffix(true, { baseVersion: 1, draftVersion: 2 })).toMatch(/^ Saving a section, or Complete, writes/);
    expect(groundTruthAlertSuffix(true, null)).toBe(' Use Start annotating, above, to correct it.');
    expect(SCREEN).toMatch(/groundTruthAlertSuffix\(editFlags\.canSaveDirectToBaseline, draft\)/);
  });

  it('no longer claims the direct save records no review', () => {
    // The direct save writes reviewed-human and counts toward progress since the
    // provenance fix; the old copy contradicted it.
    const copy = groundTruthAlertSuffix(true, { baseVersion: 1, draftVersion: 2 });
    expect(copy).not.toMatch(/rather than recording a review/);
    expect(copy).toMatch(/counts the document as reviewed/);
  });
});

describe('one route to reviewed documents at a time', () => {
  it('offers Show reviewed documents once in the rail, not again inside the empty state', () => {
    // Both showed at once when a search matched nothing. The rail and the Queue
    // complete alert's action are the two that remain — now one `showReviewedControl`
    // rendered in both, so that the progress line cannot be fixed in one and left
    // dead in the other. Counted so a third route still fails here: one definition,
    // two render sites.
    // (The reveal sets the status filter to All documents, the one filter that lists
    // completed documents; there is no separate showReviewed state on this page.)
    const definitions = SOURCE.split("setStatusFilter('all')").length - 1;
    const renderSites = SOURCE.split('showReviewedControl}').length - 1;
    expect(definitions).toBe(1);
    expect(renderSites).toBe(2);
  });
});
