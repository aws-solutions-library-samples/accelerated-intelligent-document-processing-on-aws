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
 *   change the class      -> reextractTestSetDocument -> Admin, Author, Annotator
 *   save fields (review)  -> completeSectionReview    -> Admin, Reviewer, Annotator
 *   save fields (direct)  -> uploadDocument           -> Admin, Author
 *
 * So `Author` is refused the review save and `Annotator` the direct one, while the
 * class is open to all three. Gating everything on `canAnnotate` would have invited
 * an Annotator to edit fields and lose them to an authorization error at save;
 * gating the class on the field-save flag denied it to roles the server accepts.
 * Hence a separate `canChangeClass`, and a read-only flag chosen per save path.
 *
 * The last two tests read the groups out of `schema.graphql` and check the client's
 * booleans still match, because this duplicates authorization rules the server owns
 * and the copies would otherwise drift silently.
 *
 * Asserted at source level: rendering AnnotationWorkspace needs the GraphQL client,
 * settings, role hooks, a router and a populated queue, for what is two JSX props.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

const HERE = join(__dirname, '..');
const SOURCE = readFileSync(join(HERE, 'AnnotationWorkspace.tsx'), 'utf-8');
const EDITOR = readFileSync(join(HERE, 'GroundTruthVisualEditor.tsx'), 'utf-8');
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

describe('AnnotationWorkspace edit gating', () => {
  it('picks the read-only decision from the document own save path', () => {
    // The per-path role decision, which is what this test exists to protect. It now sits
    // behind `!draft ||` — annotation is also read-only until the version transition is
    // open — but that is an orthogonal condition about the session, not about the role,
    // and the role half must stay per-save-path.
    expect(SOURCE).toMatch(/selected\.reviewObjectKey \? !canSaveViaReview : !canSaveDirectToBaseline/);
    // The regression: a queue-state condition presented as a permission problem.
    expect(SOURCE).not.toMatch(/isReadOnly=\{!canAnnotate \|\| !selected\.reviewObjectKey\}/);
    // And the over-correction: canAnnotate includes roles neither path accepts.
    expect(SOURCE).not.toMatch(/isReadOnly=\{!canAnnotate\}/);
  });

  it('does not let editing begin before the version transition is open', () => {
    // Otherwise the commitment goes back to being implicit: someone edits, and the
    // version that records what the labels *were* is opened after they changed. Looking
    // is still allowed — this gates editing, not reading.
    expect(SOURCE).toMatch(/isReadOnly=\{!draft \|\|/);
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

  it('keeps canAnnotate for page access, which is a different question', () => {
    // Whether you may open this queue at all is still Admin | Author | Annotator.
    expect(SOURCE).toMatch(/if \(!roleLoading && !canAnnotate\)/);
  });

  it('only promises class editing to someone who can actually save it', () => {
    expect(SOURCE).toMatch(/canSaveDirectToBaseline\s*\?\s*draft\s*\?\s*' You can still correct it below, including its class/);
    expect(SOURCE).toMatch(/which an Admin or Author has to do/);
  });

  it('offers the class to every role the server accepts for a class correction', () => {
    // The class is a WIDER capability than field editing: it persists through
    // reextractTestSetDocument, which stamps the baseline server-side and needs no
    // review record. Gating it on isReadOnly denied it to roles the server allows.
    expect(SOURCE).toMatch(/canChangeClass=\{canAnnotate\}/);

    const reextract = groupsFor('reextractTestSetDocument');
    expect(reextract.sort()).toEqual(['Admin', 'Annotator', 'Author']);

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
    const upload = groupsFor('uploadDocument');

    // Documents the asymmetry that makes two predicates necessary. If either of
    // these ever changes, this test fails and points at the client copy below.
    expect(review.sort()).toEqual(['Admin', 'Annotator', 'Reviewer']);
    expect(upload.sort()).toEqual(['Admin', 'Author']);

    const clientRole = (name: string): string[] => {
      const m = SOURCE.match(new RegExp(`const ${name} = ([^;]+);`));
      expect(m, `${name} not found`).not.toBeNull();
      return (m as RegExpMatchArray)[1]
        .split('||')
        .map((t) => t.trim().replace(/^is/, ''))
        .sort();
    };

    expect(clientRole('canSaveViaReview')).toEqual(review.sort());
    expect(clientRole('canSaveDirectToBaseline')).toEqual(upload.sort());
  });
});

/**
 * The prop has to be honoured, not merely accepted.
 *
 * Caught by revert-checking: with the assertions above reading only
 * AnnotationWorkspace, the editor could take `canChangeClass` and keep gating on
 * `isReadOnly` internally, and every test would still pass. That is the same silent
 * no-op as the original class-correction bug, where the UI reported success and threw
 * the change away.
 */
describe('GroundTruthVisualEditor class gating', () => {
  it('gates the class control on the class capability, not on field read-only', () => {
    expect(EDITOR).toMatch(/const mayChangeClass = canChangeClass \?\? !isReadOnly;/);
    expect(EDITOR).toMatch(/disabled=\{!mayChangeClass \|\| isReextracting\}/);
  });

  it('does not silently discard a class change it just accepted', () => {
    // updateDocumentClass guarded on isReadOnly, so with the dropdown enabled the
    // change event would be taken and dropped.
    const fn = EDITOR.slice(EDITOR.indexOf('const updateDocumentClass'), EDITOR.indexOf('const updateDocumentClass') + 400);
    expect(fn).toMatch(/if \(!mayChangeClass \|\| !localData\) return;/);
    expect(fn).not.toMatch(/if \(isReadOnly/);
  });

  it('lets the same roles trigger the re-extract that persists the class', () => {
    // The re-extract IS the class save; gating it more narrowly than the dropdown
    // would leave the correction unsaveable.
    expect(EDITOR).toMatch(/onClick=\{handleReextract\} loading=\{isReextracting\} disabled=\{!mayChangeClass\}/);
  });

  it('defaults to the old behaviour for callers that do not distinguish the two', () => {
    // TestSetDocumentDetail passes no canChangeClass, so it must be unaffected.
    const docView = readFileSync(join(HERE, 'TestSetDocumentDetail.tsx'), 'utf-8');
    expect(docView).not.toMatch(/canChangeClass/);
    expect(EDITOR).toMatch(/canChangeClass \?\? !isReadOnly/);
  });
});

/**
 * What the class control does when the class LIST cannot be read.
 *
 * `getConfigVersion` is `Admin, Author, Viewer` and excludes Annotator — the role
 * this screen exists for. So an annotator's config fetch is denied and the editor
 * used to fall through to its free-text branch, handing the one role most in need of
 * a constrained vocabulary an unconstrained box, with three words dropping out of the
 * description as the only sign. A typed class no config defines produces a section
 * with no schema, which extracts nothing.
 */
describe('GroundTruthVisualEditor class list unavailable', () => {
  it('distinguishes "could not read the classes" from "there are none"', () => {
    expect(EDITOR).toMatch(/const classListUnavailable = classOptions\.length === 0 && \(Boolean\(configError\) \|\| configLoading\)/);
    // Both signals have to be pulled off the hook, or the distinction is unavailable.
    expect(EDITOR).toMatch(/loading: configLoading, error: configError/);
  });

  it('locks the field rather than offering free text', () => {
    // Free text is for a config that genuinely defines no classes. When the list
    // merely could not be read, an unconstrained box is worse than a locked one.
    const branch = EDITOR.slice(EDITOR.indexOf('{classListUnavailable ? ('), EDITOR.indexOf('{classListUnavailable ? (') + 200);
    expect(branch).toMatch(/<Input value=\{documentClassType \?\? ''\} disabled \/>/);
  });

  it('says why, and who can do it instead', () => {
    expect(EDITOR).toMatch(/valid classes could not be loaded/);
    expect(EDITOR).toMatch(/An Admin or Author can change the class/);
  });
});
