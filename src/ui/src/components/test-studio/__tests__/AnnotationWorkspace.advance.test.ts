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
 * The queue in hand already names the next document, so the advance no longer
 * waits for anything. The two functions below are what it advances with, and the
 * source-level checks pin the ways the fix can be undone: re-awaiting the reload,
 * retiring a document the reviewer only skipped or only part-reviewed, and
 * letting an overlapping background reload replace the queue with rows that
 * predate the save that triggered it, which would make the save undo itself on
 * screen a few seconds later.
 *
 * Asserted at source level rather than by rendering, for the reason given in
 * AnnotationWorkspace.editGating.test.ts: the component needs the GraphQL
 * client, settings, role hooks, a router and a populated queue.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

import { applyReviewedToQueue, nextAvailableAfter, type QueueItem, type QueueState } from '../AnnotationWorkspace';

const SOURCE = readFileSync(join(__dirname, '..', 'AnnotationWorkspace.tsx'), 'utf-8');

const doc = (objectKey: string, overrides: Partial<QueueItem> = {}): QueueItem => ({
  objectKey,
  inputKey: `set/input/${objectKey}`,
  sectionCount: 1,
  claimedByMe: false,
  reviewed: false,
  available: true,
  labelSource: 'draft-machine',
  ...overrides,
});

const queueOf = (documents: QueueItem[], overrides: Partial<QueueState> = {}): QueueState => ({
  totalDocs: documents.length,
  reviewedDocs: 0,
  remainingDocs: documents.length,
  claimedByOthers: 0,
  documents,
  ...overrides,
});

describe('nextAvailableAfter', () => {
  it('does not reopen the document just finished, even though it is still first', () => {
    // The queue is ordered worst-first and the reply has not been refreshed, so
    // the document just saved is still at the head of the list. Taking the head
    // would hand the reviewer back the document they had finished.
    const documents = [doc('worst.pdf'), doc('next.pdf')];
    expect(nextAvailableAfter(documents, 'worst.pdf')).toBe('next.pdf');
  });

  it('skips documents the server says are not available', () => {
    // Reviewed, or claimed by another annotator: `available` already carries both.
    const documents = [doc('taken.pdf', { available: false }), doc('done.pdf', { available: false, reviewed: true }), doc('free.pdf')];
    expect(nextAvailableAfter(documents, null)).toBe('free.pdf');
  });

  it('reaches every document when skipping repeatedly, rather than bouncing between two', () => {
    // Searching from the head for "the first available document that is not the
    // open one" cannot advance: with A open it gives B, and from B it gives A
    // again, so skipping oscillates and the third document is unreachable. The
    // search runs forward from where the open document sits instead.
    const documents = [doc('A'), doc('B'), doc('C')];
    const visited: (string | null)[] = [];
    let current: string | null = 'A';
    for (let i = 0; i < 4; i += 1) {
      current = nextAvailableAfter(documents, current);
      visited.push(current);
    }
    expect(visited).toEqual(['B', 'C', 'A', 'B']);
  });

  it('wraps past the end instead of stranding the reviewer', () => {
    // The last document in the rail is not the end of the work: everything before
    // it is still unreviewed, and returning null there would empty the editor pane
    // while the queue still held documents.
    expect(nextAvailableAfter([doc('A'), doc('B'), doc('C')], 'C')).toBe('A');
  });

  it('searches from the head once the open document has left the queue', () => {
    // A save retires it, so `current` names a document the list no longer holds.
    // The head is then the right answer rather than a repeat of what was saved.
    expect(nextAvailableAfter([doc('B'), doc('C')], 'A')).toBe('B');
  });

  it('reports no next document rather than falling back to an unavailable one', () => {
    // Null closes the editor pane, which is honest. Returning a claimed document
    // would open one whose save the server refuses.
    expect(nextAvailableAfter([doc('taken.pdf', { available: false })], null)).toBeNull();
    expect(nextAvailableAfter([], null)).toBeNull();
    expect(nextAvailableAfter([doc('only.pdf')], 'only.pdf')).toBeNull();
  });
});

describe('applyReviewedToQueue', () => {
  it('retires the reviewed document from the rail and moves the counters', () => {
    const before = queueOf([doc('done.pdf'), doc('todo.pdf')]);
    const after = applyReviewedToQueue(before, 'done.pdf', false);

    expect(after.documents.map((d) => d.objectKey)).toEqual(['todo.pdf']);
    expect(after.reviewedDocs).toBe(1);
    expect(after.remainingDocs).toBe(1);
  });

  it('ticks it in place instead of hiding it when reviewed documents are shown', () => {
    // Mirrors the server's own filter: it omits reviewed documents unless
    // includeCompleted was asked for, so the rail must behave the same whether
    // this update or the reply produced the rows.
    const after = applyReviewedToQueue(queueOf([doc('done.pdf')]), 'done.pdf', true);

    expect(after.documents).toHaveLength(1);
    expect(after.documents[0]).toMatchObject({ reviewed: true, available: false, labelSource: 'reviewed-human' });
    expect(after.reviewedDocs).toBe(1);
  });

  it('leaves the queue alone for a document it does not hold', () => {
    // The rail is capped at one page, so a document reviewed from a shared ?doc=
    // link need not be on it. Counting it here would report progress against rows
    // this queue cannot show.
    const before = queueOf([doc('todo.pdf')]);
    expect(applyReviewedToQueue(before, 'elsewhere.pdf', false)).toBe(before);
  });

  it('does not move the counters twice when an already-reviewed document is saved again', () => {
    // Reopening a confirmed label and saving it is supported, and it is not new
    // progress.
    const before = queueOf([doc('done.pdf', { reviewed: true, available: false, labelSource: 'reviewed-human' })], {
      reviewedDocs: 1,
      remainingDocs: 0,
    });
    expect(applyReviewedToQueue(before, 'done.pdf', true)).toBe(before);
  });

  it('floors remaining at zero rather than reporting a negative count', () => {
    // totalDocs counts the whole set while the rail is one page, so the two can
    // disagree about how much is left.
    const after = applyReviewedToQueue(queueOf([doc('last.pdf')], { remainingDocs: 0 }), 'last.pdf', false);
    expect(after.remainingDocs).toBe(0);
  });
});

describe('the advance does not wait for the queue reload', () => {
  const body = SOURCE.slice(
    SOURCE.indexOf('const advanceToNext = useCallback('),
    SOURCE.indexOf('[loadQueue, loadImpact, selectedKey, showReviewed]'),
  );

  it('is positioned in the source where this test expects to find it', () => {
    // Guards the two assertions below from silently passing on an empty slice.
    expect(body).toContain('setSelectedKey(nextAvailableAfter(');
  });

  it('refreshes in the background instead of awaiting it', () => {
    // `loadQueue(true, true)` — preserve the selection this function just made,
    // and do not touch isLoading. An `await` here is the #1321 regression: the
    // selection would once again change only after a ~9s round trip.
    expect(body).toContain('loadQueue(true, true)');
    expect(body).not.toContain('await loadQueue');
  });

  it('skips a document without retiring it as reviewed, and without a reload', () => {
    // Two mistakes type checking cannot see, both well-typed. `reviewedKey` would
    // mark a document the reviewer deliberately skipped as reviewed and hide it
    // from the queue. And a skip changes nothing server-side, so a reload could
    // only report another annotator's claim while costing a full LIST of the
    // baseline prefix plus a read of every section of 200 documents — paid once
    // per click, since nothing serialises them any more.
    expect(SOURCE).toContain('onClick={() => advanceToNext({ refresh: false })}');
  });

  it('claims a document as reviewed only where every section of it is', () => {
    // applyReviewedToQueue's contract is a precondition on its caller, and this
    // is the caller: `isMultiSection` is what decides whether the document is
    // retired from the rail. Passing `documentName` unconditionally would tick a
    // packet after one of its sections was saved — the state the save
    // confirmation used to claim and the test set's Review state column denied.
    expect(SOURCE).toContain('advanceToNext({ reviewedKey: isMultiSection ? undefined : documentName })');
  });

  it('refreshes after a save and a confirmation, which do change server state', () => {
    // The counterpart to the skip: these two have something to collect — the
    // server's verdict on a document whose sections may now all be reviewed, and
    // the counters past the inspected page — so they must not be given the
    // skip's treatment.
    expect(SOURCE).toContain('advanceToNext({ reviewedKey: selected.objectKey })');
    expect(SOURCE).not.toMatch(/advanceToNext\(\{[^}]*refresh: false[^}]*reviewedKey/);
  });
});

describe('an overlapping reload cannot undo a save', () => {
  const body = SOURCE.slice(
    SOURCE.indexOf('const loadQueue = useCallback('),
    SOURCE.indexOf('[testSetId, requestedDoc, showReviewed, responseGuard]'),
  );

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
    // document being worked on, offering no action, and hide the completion
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
