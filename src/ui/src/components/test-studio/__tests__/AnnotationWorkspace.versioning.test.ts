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
 * Asserted at source level, following the sibling editGating test: rendering this
 * component needs the GraphQL client, settings, role hooks, a router and a populated
 * queue, for what is a handful of conditions.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

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
    expect(SOURCE).toMatch(/queue\?\.draftVersion != null/);
  });

  it('exposes the transition on the queue type, so the read is not a private contract', () => {
    const at = SCHEMA.indexOf('type AnnotationQueue ');
    expect(at).toBeGreaterThan(-1);
    const block = SCHEMA.slice(at, SCHEMA.indexOf('}', at));
    expect(block).toMatch(/draftVersion: Int/);
    expect(block).toMatch(/baseVersion: Int/);
  });

  it('asks before committing, and says what the commitment preserves', () => {
    expect(SOURCE).toMatch(/Annotating this set creates a new version of it/);
    // The reassurance is the load-bearing half: the reason it is safe to agree is that
    // the current labels survive as something scoreable.
    expect(SOURCE).toMatch(/current labels are preserved/);
    expect(SOURCE).toMatch(/Start annotating/);
  });

  it('puts the transition in the queue link', () => {
    expect(SOURCE).toMatch(/\?v=\$\{draft\.draftVersion\}/);
  });

  it('omits the version from the link before a transition exists', () => {
    // A `?v=` with nothing to name would be worse than none: it would imply a
    // transition that had not been opened.
    expect(SOURCE).toMatch(/draft\s*\n?\s*\?\s*`\$\{window\.location\.origin\}/);
  });

  it('warns when a link refers to a transition that has closed', () => {
    expect(SOURCE).toMatch(/staleLinkVersion/);
    expect(SOURCE).toMatch(/which is closed/);
    // Compared against the open draft, not merely "is a version present".
    expect(SOURCE).toMatch(/requestedVersionNumber !== draft\.draftVersion/);
    // And only an integer counts as a version: `?v=abc` used to warn about "version abc".
    expect(SOURCE).toMatch(/Number\.isInteger\(Number\(requestedVersion\)\)/);
  });

  it('keeps the transition visible while working, not only at the prompt', () => {
    // Which version a correction landed in is the question asked later, when a run's
    // score is attributed to one.
    expect(SOURCE).toMatch(/v\{draft\.baseVersion\} &rarr; v\{draft\.draftVersion\}/);
  });

  it('gates confirming labels on the transition, not only editing them', () => {
    // Found by using the screen as an annotator: the editor was read-only before a
    // transition existed, but "Labels are correct — mark reviewed" sat next to it
    // enabled. It calls completeSectionReview for every section, which tags each one
    // reviewed-human server-side — the draft-machine -> reviewed-human change a version
    // is supposed to bracket. So an entire set could be confirmed, and its provenance
    // rewritten, with no version recording what the labels had been: the silent
    // commitment this feature removes, still reachable by the button beside the one
    // that was fixed.
    expect(SOURCE).toMatch(/disabled=\{isLoading \|\| !selected\.reviewObjectKey \|\| !draft\}/);
  });

  it('leaves one primary action, and it is the one that unblocks the rest', () => {
    // Before a transition is open, neither claiming nor confirming is the next step —
    // "Start annotating" is, and it is the primary inside that alert. Claim was
    // unconditionally primary, so two solid-blue buttons competed while the actual
    // prerequisite sat lower down in an alert.
    expect(SOURCE).toMatch(/variant=\{draft \? 'primary' : 'normal'\}/);
    expect(SOURCE).not.toMatch(/<Button variant="primary" onClick=\{claimSelected\}/);
  });
});
