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
 * review-queue record, so the editor went read-only regardless of role.
 *
 * Nor can the fix be a single role predicate, because THREE server capabilities are
 * in play and no two accept the same groups:
 *
 *   change the class      -> reextractTestSetDocument   -> Admin, Author, Annotator
 *   save fields (review)  -> completeSectionReview      -> Admin, Reviewer, Annotator
 *   save fields (direct)  -> saveTestSetDocumentSection -> Admin, Author, Annotator
 *
 * The direct save is the edit screen's (TestSetItemEdit, via the test-set
 * adapter). The ground-truth editor's own fallback, used by the document view, writes
 * through `uploadDocument` (Admin, Author). So `Author` is refused the review save and
 * `Reviewer` the direct one, while the class is open to Admin, Author and Annotator. Gating everything on `canAnnotate` would have invited
 * an Annotator to edit fields and lose them to an authorization error at save;
 * gating the class on the field-save flag denied it to roles the server accepts.
 * Hence a separate `canChangeClass`, and a read-only flag chosen per save path.
 *
 * The last two tests read the groups out of `schema.graphql` and check the client's
 * booleans still match, because this duplicates authorization rules the server owns
 * and the copies would otherwise drift silently.
 *
 * The decisions are pure functions in `annotation-edit-flags.ts`, asserted directly; the
 * page-access guard is still checked against the AnnotationWorkspace source. The edit
 * screen's role gate and save path are not read as source text here: the behaviour moved
 * into the test-set item and adapter suites (`test-set-item-edit-skip.test.tsx`,
 * `test-set-item-edit-adapter.test.ts`).
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

import {
  CLASS_LIST_UNAVAILABLE_CONSTRAINT,
  CLASS_LIST_UNAVAILABLE_DESCRIPTION,
  NO_REVIEW_RECORD_MESSAGE,
  annotationEditFlags,
  groundTruthAlertSuffix,
  isClassListUnavailable,
  mayChangeClassFor,
  requireReviewObjectKey,
  saveCapabilities,
} from '../annotation-edit-flags';
import type { AnnotationRoles } from '../annotation-edit-flags';

const HERE = join(__dirname, '..');
const SOURCE = readFileSync(join(HERE, 'AnnotationWorkspace.tsx'), 'utf-8');
const SCHEMA = readFileSync(join(HERE, '..', '..', '..', '..', '..', 'nested', 'api-resolvers', 'src', 'api', 'schema.graphql'), 'utf-8');

/** The cognito_groups declared on a mutation in schema.graphql. */
const groupsFor = (operation: string): string[] => {
  const at = SCHEMA.indexOf(`\n  ${operation}(`);
  expect(at, `${operation} not found in schema.graphql`).toBeGreaterThan(-1);
  const window = SCHEMA.slice(at, at + 600);
  const match = window.match(/cognito_groups:\s*\[([^\]]+)\]/);
  expect(match, `${operation} has no cognito_groups`).not.toBeNull();
  return (match as RegExpMatchArray)[1].split(',').map((g) => g.trim().replace(/"/g, ''));
};

const GROUPS = ['Admin', 'Author', 'Reviewer', 'Annotator', 'Viewer'] as const;

/** Role flags for a caller in exactly one group; canAnnotate as useUserRole derives it. */
const only = (group: (typeof GROUPS)[number]): AnnotationRoles => {
  const flags = {
    isAdmin: group === 'Admin',
    isAuthor: group === 'Author',
    isReviewer: group === 'Reviewer',
    isAnnotator: group === 'Annotator',
  };
  return { ...flags, canAnnotate: flags.isAdmin || flags.isAuthor || flags.isAnnotator };
};

/** The groups whose single-group caller gets `pick(...)` true. */
const groupsWhere = (pick: (roles: AnnotationRoles) => boolean): string[] => GROUPS.filter((g) => pick(only(g))).sort();

const DRAFT = { baseVersion: 1, draftVersion: 2 };
const WITH_REVIEW = { reviewObjectKey: 'ts1/x.pdf' };
const AUTHORED = { reviewObjectKey: null };

describe('AnnotationWorkspace edit gating', () => {
  it('picks the read-only decision from the document own save path', () => {
    // A document with a review record: edited (as a draft, then completed) by an Admin or
    // an Annotator. The draft operation has no Reviewer and completion has no Author.
    expect(groupsWhere((r) => !annotationEditFlags(r, WITH_REVIEW, DRAFT).isReadOnly)).toEqual(['Admin', 'Annotator']);
    // Authored ground truth: the direct path's roles may edit (saveTestSetDocumentSection,
    // which accepts an Annotator for a document assigned to them). The regression made this
    // read-only for everyone — a queue-state condition presented as a permission problem.
    expect(groupsWhere((r) => !annotationEditFlags(r, AUTHORED, DRAFT).isReadOnly)).toEqual(['Admin', 'Annotator', 'Author']);
  });

  it('does not let editing begin before the version transition is open', () => {
    // Otherwise the commitment goes back to being implicit. Looking is still allowed —
    // this gates editing, not reading.
    for (const group of GROUPS) {
      expect(annotationEditFlags(only(group), WITH_REVIEW, null).isReadOnly).toBe(true);
      expect(annotationEditFlags(only(group), AUTHORED, null).isReadOnly).toBe(true);
    }
  });

  it('routes through the review API only when there is a review to complete', () => {
    // completeSectionReview requires reviewObjectKey and throws without it, so wiring
    // it unconditionally would lose the edit at save time.
    expect(annotationEditFlags(only('Admin'), WITH_REVIEW, DRAFT).saveViaReview).toBe(true);
    expect(annotationEditFlags(only('Admin'), AUTHORED, DRAFT).saveViaReview).toBe(false);
  });

  it('still refuses to save a review without a review record', () => {
    // The backstop if the routing above is ever changed back.
    expect(() => requireReviewObjectKey(AUTHORED)).toThrow(NO_REVIEW_RECORD_MESSAGE);
    expect(() => requireReviewObjectKey(null)).toThrow(NO_REVIEW_RECORD_MESSAGE);
    expect(requireReviewObjectKey(WITH_REVIEW)).toBe('ts1/x.pdf');
  });

  it('keeps canAnnotate for page access, which is a different question', () => {
    // Whether you may open this queue, or one of its documents, at all is still
    // Admin | Author | Annotator.
    expect(SOURCE).toMatch(/if \(!roleLoading && !canAnnotate\)/);
  });

  it('only promises editing to someone who can actually save it', () => {
    // The edit screen's alert already says "correct them if needed, then
    // Complete"; the suffix tells a saver what saving does, and anyone else who can.
    expect(groundTruthAlertSuffix(true, DRAFT)).toBe(
      ' Save keeps your changes; Process Changes writes the ground truth and counts the document as reviewed.',
    );
    const refused = groundTruthAlertSuffix(false, DRAFT);
    expect(refused).toMatch(/an Admin or an Author can change it/);
    expect(refused).not.toMatch(/Saving|Complete|correct it/);
    // The same for someone who cannot save, whether or not a transition is open.
    expect(groundTruthAlertSuffix(false, null)).toBe(refused);
  });

  it('offers the class to every role the server accepts for a class correction', () => {
    // The class is a WIDER capability than field editing: it persists through
    // reextractTestSetDocument, which stamps the baseline server-side and needs no
    // review record. Gating it on isReadOnly denied it to roles the server allows.
    const reextract = groupsFor('reextractTestSetDocument');
    expect(reextract.sort()).toEqual(['Admin', 'Annotator', 'Author']);
    expect(groupsWhere((r) => annotationEditFlags(r, AUTHORED, DRAFT).canChangeClass)).toEqual(reextract.sort());

    // canAnnotate is that same set, which is why it is the right flag here.
    const roleHook = readFileSync(join(HERE, '..', '..', 'hooks', 'use-user-role.ts'), 'utf-8');
    const m = roleHook.match(/const canAnnotate = ([^;]+);/);
    expect(m).not.toBeNull();
    const roles = (m as RegExpMatchArray)[1]
      .split('||')
      .map((t) => t.trim().replace(/^is/, ''))
      .sort();
    expect(roles).toEqual(reextract.sort());
  });

  it("matches the schema's groups, so the duplicated rule cannot drift", () => {
    const review = groupsFor('completeSectionReview');
    const direct = groupsFor('saveTestSetDocumentSection');

    // Documents the asymmetry that makes two predicates necessary. If either of
    // these ever changes, this test fails and points at the client copy below.
    expect(review.sort()).toEqual(['Admin', 'Annotator', 'Reviewer']);
    expect(direct.sort()).toEqual(['Admin', 'Annotator', 'Author']);

    expect(groupsWhere((r) => saveCapabilities(r).canSaveViaReview)).toEqual(review.sort());
    expect(groupsWhere((r) => saveCapabilities(r).canSaveDirectToBaseline)).toEqual(direct.sort());
  });

  it('matches the draft operation’s groups, so the review-copy rule cannot drift', () => {
    // saveTestSetDocumentSectionDraft is Admin, Author, Annotator; an Author is refused a
    // review copy server-side, which is why a review copy is editable only by the
    // intersection with completeSectionReview's groups.
    const draft = groupsFor('saveTestSetDocumentSectionDraft').sort();
    expect(draft).toEqual(['Admin', 'Annotator', 'Author']);
    expect(groupsWhere((r) => saveCapabilities(r).canDraft)).toEqual(draft);
    const review = groupsFor('completeSectionReview');
    expect(groupsWhere((r) => !annotationEditFlags(r, WITH_REVIEW, DRAFT).isReadOnly)).toEqual(
      draft.filter((g) => review.includes(g)).sort(),
    );
  });

  it('saves the direct path through the mutation those groups describe', () => {
    // canSaveDirectToBaseline mirrors saveTestSetDocumentSection only because the
    // edit screen always hands the editor its own save. If it stopped passing
    // onSave, the editor would fall back to uploadDocument (Admin, Author) and an
    // Annotator would lose the edit to an authorization error.
    expect(groupsFor('uploadDocument').sort()).toEqual(['Admin', 'Author']);
  });
});

/**
 * The class capability has to be honoured, not merely accepted: the editor could take
 * `canChangeClass` and keep gating on `isReadOnly` internally, the same silent no-op as
 * the original class-correction bug, where the UI reported success and threw the change
 * away.
 */
describe('the Test Studio class gating', () => {
  it('gates the class control on the class capability, not on field read-only', () => {
    expect(mayChangeClassFor(true, true)).toBe(true);
    expect(mayChangeClassFor(false, false)).toBe(false);
  });

  it('defaults to the old behaviour for callers that do not distinguish the two', () => {
    expect(mayChangeClassFor(undefined, true)).toBe(false);
    expect(mayChangeClassFor(undefined, false)).toBe(true);
  });
});

/**
 * What the class control does when the class LIST cannot be read. A denied or failed
 * fetch must not fall through to free text: a typed class no config defines produces a
 * section with no schema, which extracts nothing.
 */
describe('the Test Studio class list unavailable', () => {
  it('distinguishes "could not read the classes" from "there are none"', () => {
    expect(isClassListUnavailable(0, new Error('denied'), false)).toBe(true);
    expect(isClassListUnavailable(0, null, true)).toBe(true);
    expect(isClassListUnavailable(0, null, false)).toBe(false);
    expect(isClassListUnavailable(2, new Error('denied'), false)).toBe(false);
  });

  it('says why, and who can do it instead', () => {
    expect(CLASS_LIST_UNAVAILABLE_DESCRIPTION).toMatch(/valid classes could not be loaded/);
    expect(CLASS_LIST_UNAVAILABLE_CONSTRAINT).toMatch(/An Admin or Author can change the class/);
  });
});
