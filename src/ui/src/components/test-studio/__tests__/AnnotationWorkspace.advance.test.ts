// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Moving to the next document after a save.
 *
 * Reported as [#1321] — "after clicking 'Save and next Document in queue' the
 * document status doesn't update". Measured on a live stack, the status was
 * recorded correctly every time: each save appeared in the very next
 * `getAnnotationQueue` reply (2000 → 1999 → 1998 → 1997 → 1996 remaining, each
 * immediately after the corresponding baseline write). What was wrong was the
 * wait. `advanceToNext` awaited a full queue reload before changing the
 * selection, and that call measured 8.6–9.6s on a 2000-document set, because
 * `getAnnotationQueue` lists the whole `baseline/` prefix and then reads every
 * section of all 200 ranked documents from S3. So the editor sat on the document
 * it had just saved for the better part of ten seconds, which reads as a save
 * that did nothing.
 *
 * The edit screen now leaves the document as soon as its mutation lands and
 * the queue page reads the queue on arrival, so nothing waits on that reply. The
 * source-level checks pin the ways the fix can be undone: re-awaiting a reload
 * before leaving, announcing a document reviewed after only part of it was saved,
 * and letting an overlapping reload of the queue page replace its rows with an
 * older reply.
 *
 * Asserted at source level rather than by rendering, for the reason given in
 * AnnotationWorkspace.editGating.test.ts: the component needs the GraphQL
 * client, settings, role hooks, a router and a populated queue.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

const SOURCE = readFileSync(join(__dirname, '..', 'AnnotationWorkspace.tsx'), 'utf-8');
// The per-document screen this page opens. Saving, completing and skipping happen
// there, not in an in-page rail, so that is where the #1321 behaviour lives now.
const SCREEN = readFileSync(join(__dirname, '..', '..', 'edit-screen', 'TestSetItemEdit.tsx'), 'utf-8');

/*
 * Develop's `nextAvailableAfter` / `applyReviewedToQueue` unit tests are not carried
 * here. They pinned the in-page "Save & next in queue" rail, which this page no longer
 * has: it is a queue table, and each row opens the per-document edit screen
 * (edit-screen/TestSetItemEdit.tsx), which returns to the queue (or Home › Tasks)
 * after Complete, Skip or the last section's save. There is no in-page advance, no
 * local next-document choice and no local retirement of a row left to test. The
 * intent of those tests — leave the saved document at once, and never claim a
 * part-reviewed packet as reviewed — is pinned below against the screen that owns it.
 */
describe('leaving a document does not wait for the queue reload', () => {
  const handleAction = SCREEN.slice(SCREEN.indexOf('const handleAction = async'), SCREEN.indexOf('const handleSaved ='));
  const handleSaved = SCREEN.slice(SCREEN.indexOf('const handleSaved ='), SCREEN.indexOf('const notices ='));

  it('is positioned in the source where this test expects to find it', () => {
    // Guards the assertions below from silently passing on an empty slice.
    expect(handleAction).toContain('await adapter.skip(reason, note)');
    expect(handleSaved).toContain('savedSections.current.add(sectionId)');
  });

  it('exits as soon as the mutation lands, without awaiting a queue reload', () => {
    // The #1321 regression was the editor sitting on the document it had just saved
    // while a ~9s getAnnotationQueue reply came back. The exit navigates straight
    // away; the queue page reads the queue itself on arrival.
    const success = handleAction.slice(0, handleAction.indexOf('} catch'));
    expect(success).toMatch(/exit\(\s*queuePath,/);
    expect(success).not.toContain('await reload');
    expect(handleSaved).not.toContain('await reload');
  });

  it('claims a document as reviewed only where every section of it is', () => {
    // The server marks a document reviewed only once every section carries a human
    // verdict, so one section's save on a packet must not announce the document
    // reviewed — the claim the test set's own Review state column contradicted.
    const everyAt = handleSaved.indexOf('if (sections.every((s) => savedSections.current.has(s.sectionId)))');
    expect(everyAt).toBeGreaterThan(-1);
    expect(handleSaved.indexOf('is marked reviewed')).toBeGreaterThan(everyAt);
    expect(handleSaved).toContain('Check the other sections, then choose Process Changes.');
  });

  it('does not report a skipped document as reviewed', () => {
    // A skip is not a verdict: it gives the document back to its queue.
    expect(handleAction).toContain('returned to the queue.');
    expect(handleAction).not.toContain('is complete (skipped).');
  });
});

describe('an overlapping reload cannot undo a save', () => {
  const body = SOURCE.slice(SOURCE.indexOf('const loadQueue = useCallback('), SOURCE.indexOf('[testSetId, showReviewed, responseGuard]'));

  it('is positioned in the source where this test expects to find it', () => {
    // Guards the assertions below from passing vacuously on an empty slice, and
    // names loadQueue's dependency list, which is the slice's end marker.
    expect(body).toContain('query: getAnnotationQueue');
  });

  it('claims the response guard before awaiting, and discards a superseded reply', () => {
    // Background refreshes make overlapping reloads ordinary: a reload started
    // before a save lands after it, carrying rows that predate it, and a
    // `setQueue` with those rows puts the saved document back in the rail as
    // outstanding with the counters rolled back. The claim has to happen BEFORE
    // the await or precedence is decided by which reply finished last, which is
    // the thing being guarded against. Same guard, same reason, as the test set
    // documents table.
    const claimAt = body.indexOf('responseGuard.claim()');
    const awaitAt = body.indexOf('await client.graphql');
    expect(claimAt).toBeGreaterThan(-1);
    expect(claimAt).toBeLessThan(awaitAt);
    expect(body).toContain('if (!isCurrentLoad()) return;');
  });

  it('does not raise the page banner for a refresh nobody asked for', () => {
    // The error Alert is not dismissible and the "Queue complete" alert is gated
    // on there being no error, so a background failure would sit over the
    // queue being worked on, offering no action, and hide the completion
    // message. Every setError on this path is therefore conditional.
    expect(body).toMatch(/if \(!background\) \{\s*foregroundLoads\.current \+= 1;\s*setIsLoading\(true\);\s*setError\(null\);\s*\}/);
    expect(body).toContain("if (!background) setError('The annotation queue could not be loaded.')");
    expect(body).toContain('if (!background && isCurrentLoad()) {');
    // Counted, so a fourth call added later has to be guarded too rather than
    // slipping past three assertions that each still pass.
    expect(body.match(/setError\(/g) ?? []).toHaveLength(3);
  });

  it('lets the last foreground load clear the spinner, and no other', () => {
    // The spinner needs the same protection as the rows: a superseded reply
    // clearing it reports "loaded" while the reply that will actually be shown
    // is still in flight — and on this screen that also empties the reveal
    // control, which renders the progress line only while isLoading, so it
    // vanishes mid-load leaving nothing in its place.
    //
    // ⚠️ But NOT via isCurrentLoad(). A background load claims the same guard,
    // so it can supersede a foreground one; the foreground then declines to
    // clear a spinner no background load clears either, and isLoading sticks
    // true with Claim, Release, Confirm and Skip disabled for good. Hence the
    // in-flight count. This asserts the shape that cannot deadlock, and the
    // count of setIsLoading calls, so a fourth is not added unguarded — the
    // ratchet the setError assertion above has and this state did not.
    expect(body).toContain('foregroundLoads.current += 1');
    expect(body).toContain('foregroundLoads.current = Math.max(0, foregroundLoads.current - 1)');
    expect(body).toContain('if (foregroundLoads.current === 0) setIsLoading(false)');
    expect(body).not.toMatch(/isCurrentLoad\(\)\)\s*setIsLoading\(false\)/);
    expect(body.match(/setIsLoading\(/g) ?? []).toHaveLength(2);
  });
});
