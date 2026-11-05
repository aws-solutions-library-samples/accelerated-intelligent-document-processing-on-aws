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
 * The decisions are pure helpers in `annotation-edit-flags.ts`. The rest is checked
 * against the component source: the queue page (AnnotationWorkspace) carries the link
 * and its stale-version warning, and the edit screen
 * (TestSetItemEdit, whose model is over the test-set adapter) carries the draft prompt and
 * Process Changes, which opens the version behind a confirmation.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

import { DRAFT_PROMPT_BODY, DRAFT_PROMPT_HEADER, annotationQueueLink, currentDraft, staleLinkVersion } from '../annotation-edit-flags';

const DRAFT = { baseVersion: 1, draftVersion: 2 };

const HERE = join(__dirname, '..');
const SOURCE = readFileSync(join(HERE, 'AnnotationWorkspace.tsx'), 'utf-8');
const SCREEN = readFileSync(join(HERE, '..', 'edit-screen', 'test-set-item-edit-model.tsx'), 'utf-8');
const ADAPTER = readFileSync(join(HERE, '..', 'edit-screen', 'test-set-item-edit-adapter.ts'), 'utf-8');
const QUERY = readFileSync(join(HERE, '..', '..', 'graphql', 'operations', 'queries', 'GetAnnotationQueue.graphql'), 'utf-8');
const SCHEMA = readFileSync(join(HERE, '..', '..', '..', '..', '..', 'nested', 'api-resolvers', 'src', 'api', 'schema.graphql'), 'utf-8');

describe('AnnotationWorkspace version transition', () => {
  it('does not open a transition just by loading the page', () => {
    // openTestSetAnnotationDraft snapshots the baselines, so calling it to *discover*
    // whether a draft exists would commit on arrival — the exact behaviour being fixed.
    // It may only be called from the explicit handler. The queue page does not call it at
    // all; the adapter wraps it once, and the screen calls that only from Process Changes.
    expect(SOURCE).not.toMatch(/openTestSetAnnotationDraft|openDraft\(/);
    const calls = ADAPTER.split('openTestSetAnnotationDraft').length - 1;
    // The import, the mutation call, and reading it off the response.
    expect(calls).toBeLessThanOrEqual(3);
    expect(ADAPTER).not.toMatch(/useEffect\([^)]*openDraft/);
    // One explicit call site on the screen: Process Changes, which opens the transition
    // itself when none is open (see the primary action test below).
    const screenCalls = SCREEN.match(/\bopenDraft\(\)/g) ?? [];
    expect(screenCalls).toHaveLength(1);
    expect(SCREEN).not.toMatch(/handleOpenDraft/);
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
    // What each step writes: Save a draft, Process Changes the next version (the current
    // one preserved) as reviewed ground truth, Publish version freezes it.
    expect(DRAFT_PROMPT_HEADER).toBe('Save keeps a draft');
    expect(DRAFT_PROMPT_BODY).toBe(
      'Process Changes opens the next version of this set (the current version stays as it is) and writes these labels as reviewed ground truth. Publish version on the test set freezes it.',
    );
    // Shown, with no action, while no draft version is open and the user may edit.
    expect(SCREEN).toMatch(
      /!draft &&\s*!editorReadOnly && \{\s*id: 'draft',[\s\S]*?<b>\{DRAFT_PROMPT_HEADER\}\.<\/b> \{DRAFT_PROMPT_BODY\}/,
    );
    // Process Changes asks for the version, while none is open, with a confirmation.
    expect(SCREEN).toMatch(/confirm: draft \? undefined : \{ kind: 'version' \}/);
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
    // The queue page, where Copy queue link points, reads ?v= and says so.
    expect(SOURCE).toMatch(/searchParams\.get\('v'\)/);
    expect(SOURCE).toMatch(/staleVersionFor\(requestedVersion, draft\)/);
    expect(SOURCE).toMatch(
      /\{staleLinkVersion && \(\s*<Alert type="warning" header=\{`This link refers to version \$\{staleLinkVersion\}, which is closed`\}>/,
    );
    // Compared against the open draft, not merely "is a version present".
    expect(staleLinkVersion('2', DRAFT)).toBeNull();
    expect(staleLinkVersion('1', null)).toBeNull();
    // And only an integer counts as a version: `?v=abc` used to warn about "version abc".
    expect(staleLinkVersion('abc', DRAFT)).toBeNull();
    expect(staleLinkVersion(null, DRAFT)).toBeNull();
  });

  it('shows the transition on the queue page and in the edit header, with one badge', () => {
    // Which version a correction lands in is asked while editing too, so the edit header
    // shows the same badge as the queue page while a draft version is open.
    expect(SOURCE).toMatch(/<VersionBadge text=\{versionBadgeText\(draft\)\} hover=\{draftBadgeHover\(draft\)\} \/>/);
    expect(SCREEN).toMatch(/badge: draft \? \{ text: versionBadgeText\(draft\), hover: draftBadgeHover\(draft\) \} : undefined/);
  });

  it('lets the primary action open the transition itself, only when none is open, and only on click', () => {
    // HITL review queues (IDP-0002 R2-N): the primary action needs no edit first. Choosing it is the explicit act that opens the version transition, so
    // the commitment is still never made on arrival - only from the primary action's
    // handler, and only when no transition is open yet, before the sections are applied.
    const handler = SCREEN.slice(SCREEN.indexOf('      process: {'), SCREEN.indexOf('      cancelTo:'));
    expect(handler).toMatch(/if \(!draft\) await adapter\.openDraft\(\);\s*const \{ reviewed \} = await adapter\.applyAndComplete\(\);/);
    // Only the role half of the editor's read-only state disables the primary action.
    expect(SCREEN).not.toMatch(/completeReason = [^;]*draft/);
    expect(SCREEN).toMatch(/primaryDisabledReason: completeReason/);
  });
});
