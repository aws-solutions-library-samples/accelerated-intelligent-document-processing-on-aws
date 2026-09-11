// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * A queue an annotator cannot unblock, and what the card says about it.
 *
 * Found on an annotator account: one assigned set was badged "Needs labeling first"
 * above a link reading "Open queue — most confidence alerts first". Generating those
 * labels is `generateDraftLabels`, which is Admin/Author only, so the blocker was
 * stated to the one role that cannot clear it, with no one named to take it to and a
 * link promising an ordered queue that opens empty.
 *
 * The page already had the right pattern a few lines up — "Ask an administrator to
 * assign you a test set — they can do this from User Management" for the no-sets
 * case — so this is that same courtesy extended to the next blocker along.
 *
 * The role split is asserted against the RBAC expectations file rather than restated,
 * because a copied rule is a rule that drifts.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

const HERE = join(__dirname, '..');
const SOURCE = readFileSync(join(HERE, 'AnnotationQueueLanding.tsx'), 'utf-8');
const ROLE_HOOK = readFileSync(join(HERE, '..', '..', 'hooks', 'use-user-role.ts'), 'utf-8');
const RBAC = readFileSync(join(HERE, '..', '..', '..', '..', '..', 'scripts', 'api_rbac_expectations.yaml'), 'utf-8');

/** The `groups:` list declared for an operation in api_rbac_expectations.yaml. */
const groupsFor = (operation: string): string[] => {
  const at = RBAC.indexOf(`\n  ${operation}:`);
  expect(at, `${operation} not found in api_rbac_expectations.yaml`).toBeGreaterThan(-1);
  const match = RBAC.slice(at, at + 400).match(/groups:\s*\[([^\]]+)\]/);
  expect(match, `${operation} has no groups list`).not.toBeNull();
  return (match as RegExpMatchArray)[1]
    .split(',')
    .map((g) => g.trim().replace(/['"]/g, ''))
    .sort();
};

describe('a test set with no reviewable work', () => {
  it('names who can unblock it when the reader cannot', () => {
    expect(SOURCE).toMatch(/Ask an administrator or author to generate draft labels for this set\./);
  });

  it('tells someone who CAN do it that it is theirs to do', () => {
    // Admins and Authors reach this same landing page; sending them to ask an
    // administrator would be a dead end of its own.
    expect(SOURCE).toMatch(/Generate draft labels for this set to create review work\./);
  });

  it('decides that on the capability that actually governs the call', () => {
    expect(SOURCE).toMatch(/canWrite\s*$/m);
    // canWrite must be exactly generateDraftLabels' permitted groups, or the card
    // offers the action to a role the server refuses (or hides it from one it allows).
    const permitted = groupsFor('generateDraftLabels');
    expect(permitted).toEqual(['Admin', 'Author']);

    const m = ROLE_HOOK.match(/const canWrite = ([^;]+);/);
    expect(m).not.toBeNull();
    const roles = (m as RegExpMatchArray)[1]
      .split('||')
      .map((t) => t.trim().replace(/^is/, ''))
      .sort();
    expect(roles).toEqual(permitted);
  });

  it('stops promising an ordered queue it cannot produce', () => {
    // The link stays — a blocked set is still worth opening — but "most confidence
    // alerts first" describes work that does not exist yet.
    expect(SOURCE).toMatch(/workable \? 'Open queue — most confidence alerts first' : 'Open set'/);
  });

  it('still says that for a set whose summary has not loaded', () => {
    // Absent summary must read as workable, or every card flickers through "Open set"
    // on the way in and looks blocked when it is not.
    expect(SOURCE).toMatch(/const workable = !summary \|\| summary\.hasReviewableWork;/);
  });
});
