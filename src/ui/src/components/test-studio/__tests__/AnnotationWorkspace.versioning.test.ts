// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The version transition an annotation session belongs to, in the UI.
 *
 * Starting annotation on a set commits to a new version of it, even when the set already
 * has ground truth, and the queue link should name that transition.
 *
 * Three properties follow, and each is here because getting it wrong reintroduces the
 * silent commitment he objected to:
 *
 *   1. the transition is asked for, not opened by arriving on the page;
 *   2. an existing transition is *read*, so a returning reviewer is not asked twice and
 *      the baselines are not re-snapshotted;
 *   3. the queue link names its transition, and a link from a closed one says so.
 *
 * The decisions are pure helpers in `annotation-edit-flags.ts`; the "never opened on
 * arrival" property is still checked against the component source.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

import {
  DRAFT_PROMPT_BODY,
  DRAFT_PROMPT_HEADER,
  annotationQueueLink,
  currentDraft,
  documentActionState,
  staleLinkVersion,
} from '../annotation-edit-flags';

const DRAFT = { baseVersion: 1, draftVersion: 2 };
const UNCLAIMED = { reviewObjectKey: 'ts1/x.pdf', claimedByMe: false, reviewed: false };

const HERE = join(__dirname, '..');
const SOURCE = readFileSync(join(HERE, 'AnnotationWorkspace.tsx'), 'utf-8');
const QUERY = readFileSync(join(HERE, '..', '..', 'graphql', 'operations', 'queries', 'GetAnnotationQueue.graphql'), 'utf-8');
const SCHEMA = readFileSync(join(HERE, '..', '..', '..', '..', '..', 'nested', 'api-resolvers', 'src', 'api', 'schema.graphql'), 'utf-8');

describe('AnnotationWorkspace version transition', () => {
  it('does not open a transition just by loading the page', () => {
    // openTestSetAnnotationDraft snapshots the baselines, so calling it to *discover*
    // whether a draft exists would commit on arrival — the exact behaviour being fixed.
    // It may only be called from the explicit handler.
    const calls = SOURCE.split('openTestSetAnnotationDraft').length - 1;
    // The import, the mutation call, and reading it off the response.
    expect(calls).toBeLessThanOrEqual(3);
    expect(SOURCE).not.toMatch(/useEffect\([^)]*openDraft/);
  });

  it('reads an existing transition from the queue instead of probing for it', () => {
    // Which is why these fields are on the query at all.
    expect(QUERY).toMatch(/^\s*draftVersion$/m);
    expect(QUERY).toMatch(/^\s*baseVersion$/m);
    expect(currentDraft({ draftVersion: 3, baseVersion: 2 }, null)).toEqual({ baseVersion: 2, draftVersion: 3 });
    // A baseVersion the server omitted is the one before the draft.
    expect(currentDraft({ draftVersion: 3 }, null)).toEqual({ baseVersion: 2, draftVersion: 3 });
    // Before the next queue refresh, the transition just opened.
    expect(currentDraft({ draftVersion: null }, DRAFT)).toBe(DRAFT);
    expect(currentDraft(null, null)).toBeNull();
  });

  it('exposes the transition on the queue type, so the read is not a private contract', () => {
    const at = SCHEMA.indexOf('type AnnotationQueue ');
    expect(at).toBeGreaterThan(-1);
    const block = SCHEMA.slice(at, SCHEMA.indexOf('}', at));
    expect(block).toMatch(/draftVersion: Int/);
    expect(block).toMatch(/baseVersion: Int/);
  });

  it('asks before committing, and says what the commitment preserves', () => {
    expect(DRAFT_PROMPT_HEADER).toMatch(/Annotating this set creates a new version of it/);
    // The reassurance is the load-bearing half: the reason it is safe to agree is that
    // the current labels survive as something scoreable.
    expect(DRAFT_PROMPT_BODY).toMatch(/current labels are preserved/);
    expect(SOURCE).toMatch(/Start annotating/);
  });

  it('puts the transition in the queue link', () => {
    expect(annotationQueueLink('https://x.test', '#/test-studio/sets/ts1/annotate', DRAFT)).toBe(
      'https://x.test/#/test-studio/sets/ts1/annotate?v=2',
    );
  });

  it('omits the version from the link before a transition exists', () => {
    // A `?v=` with nothing to name would be worse than none: it would imply a
    // transition that had not been opened.
    expect(annotationQueueLink('https://x.test', '#/test-studio/sets/ts1/annotate', null)).toBe(
      'https://x.test/#/test-studio/sets/ts1/annotate',
    );
  });

  it('warns when a link refers to a transition that has closed', () => {
    expect(staleLinkVersion('1', DRAFT)).toBe(1);
    expect(SOURCE).toMatch(/which is closed/);
    // Compared against the open draft, not merely "is a version present".
    expect(staleLinkVersion('2', DRAFT)).toBeNull();
    expect(staleLinkVersion('1', null)).toBeNull();
    // And only an integer counts as a version: `?v=abc` used to warn about "version abc".
    expect(staleLinkVersion('abc', DRAFT)).toBeNull();
    expect(staleLinkVersion(null, DRAFT)).toBeNull();
  });

  it('keeps the transition visible while working, not only at the prompt', () => {
    // Which version a correction landed in is the question asked later, when a run's
    // score is attributed to one.
    expect(SOURCE).toMatch(/v\{draft\.baseVersion\} &rarr; v\{draft\.draftVersion\}/);
  });

  it('gates confirming labels on the transition, not only editing them', () => {
    // Confirming calls completeSectionReview for every section, which tags each one
    // reviewed-human server-side — the draft-machine -> reviewed-human change a version
    // is supposed to bracket. With the button enabled before a transition existed, an
    // entire set could be confirmed, and its provenance rewritten, with no version
    // recording what the labels had been.
    expect(documentActionState(UNCLAIMED, null, false).confirmDisabled).toBe(true);
    expect(documentActionState(UNCLAIMED, DRAFT, false).confirmDisabled).toBe(false);
    expect(documentActionState(UNCLAIMED, DRAFT, true).confirmDisabled).toBe(true);
    expect(documentActionState({ ...UNCLAIMED, reviewObjectKey: null }, DRAFT, false).confirmDisabled).toBe(true);
  });

  it('leaves one primary action, and it is the one that unblocks the rest', () => {
    // Before a transition is open, neither claiming nor confirming is the next step —
    // "Start annotating" is, and it is the primary inside that alert.
    const before = documentActionState(UNCLAIMED, null, false);
    expect(before.claimVariant).toBe('normal');
    expect(before.confirmVariant).toBe('normal');
    // Then claim an unclaimed document, then confirm the one you hold.
    expect(documentActionState(UNCLAIMED, DRAFT, false)).toMatchObject({ claimVariant: 'primary', confirmVariant: 'normal' });
    expect(documentActionState({ ...UNCLAIMED, claimedByMe: true }, DRAFT, false).confirmVariant).toBe('primary');
  });
});
