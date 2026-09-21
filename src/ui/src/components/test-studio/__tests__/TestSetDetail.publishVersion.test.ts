// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Publishing a version happens on the test set's own page (GitHub #903).
 *
 * That is where the pass it completes is run: a user generates draft labels, reviews
 * them, and then freezes the result. From the table it was a one-click item in a menu
 * that acts on a table row, several clicks away from the set it published.
 *
 * There is one path, not two. `publishTestSetVersion` takes a single `testSetId` and
 * the table's item was disabled unless exactly one row was selected, so the table
 * could not publish in bulk and nothing is lost by removing it. The table keeps its
 * Version column: it reads versions across every set, which is what a list is for.
 *
 * Asserted at source level, following the sibling tests in this directory — the page
 * needs the GraphQL client, settings, role and generator hooks to render. The dialog
 * itself is render-tested in PublishVersionModal.test.tsx.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

const HERE = join(__dirname, '..');
const ROOT = join(HERE, '..', '..', '..', '..', '..');
const DETAIL = readFileSync(join(HERE, 'TestSetDetail.tsx'), 'utf-8');
const TEST_SETS = readFileSync(join(HERE, 'TestSets.tsx'), 'utf-8');
const ROLE = readFileSync(join(HERE, '..', '..', 'hooks', 'use-user-role.ts'), 'utf-8');
const RBAC = readFileSync(join(ROOT, 'scripts', 'api_rbac_expectations.yaml'), 'utf-8');
const SCHEMA = readFileSync(join(ROOT, 'nested', 'api-resolvers', 'src', 'api', 'schema.graphql'), 'utf-8');

describe('publishing a version from the set detail page', () => {
  it('offers the control and the dialog behind it', () => {
    expect(DETAIL).toMatch(/<PublishVersionModal/);
    expect(DETAIL).toMatch(/onClick=\{openPublishDialog\}/);
    expect(DETAIL).toMatch(/query: publishTestSetVersion/);
  });

  /**
   * The dialog and the docs tell the user what moving the active reference does, at
   * the moment they choose whether to move it, so a wrong description here is more
   * costly than anywhere else and the wording is pinned.
   *
   * The behaviour behind it is guarded where it lives, in
   * `lib/idp_common_pkg/tests/unit/test_test_runner_rbac.py`:
   * `test_scores_current_labels_by_default_not_the_active_reference` seeds
   * `activeReference: 2` and asserts current labels are used anyway. Nothing here
   * asserts against `TestRunner.tsx` — the default lives in the backend runner,
   * which that file would never mention, so such a check would keep passing if the
   * default moved, and would fail on a version dropdown that merely *labelled* the
   * pinned option "active reference", which would still be true copy.
   */
  it('describes the active reference in the dialog and the docs without the scoring claim', () => {
    const MODAL = readFileSync(join(HERE, 'PublishVersionModal.tsx'), 'utf-8');
    const DOC = readFileSync(join(HERE, '..', '..', '..', '..', '..', 'docs', 'test-studio.md'), 'utf-8');
    expect(MODAL).toMatch(/does not decide what a test run is scored against/);
    expect(MODAL).not.toMatch(/active reference is the baseline/);
    expect(DOC).toMatch(/active reference does not decide what a test run is scored against/);
    // The section's framing metaphor, well above that note, carried the same claim —
    // so the positive assertion passed while the page contradicted itself.
    expect(DOC).not.toMatch(/the tag that scoring\s+follows/);

    // The second published page carries the same correction and was otherwise
    // unpinned, so it could drift back on its own.
    const SETUP_DOC = readFileSync(join(HERE, '..', '..', '..', '..', '..', 'docs', 'creating-custom-test-sets.md'), 'utf-8');
    expect(SETUP_DOC).toMatch(/which version a run is scored against is chosen in the runner/);
    expect(SETUP_DOC).not.toMatch(/so every subsequent test run records which/);
  });

  /**
   * A publish that reports an error may still have succeeded: the copy can outlast the
   * dispatcher's request budget. The retry therefore has to be recognisable as the same
   * attempt, or it makes a second version and a second full copy of the labels.
   *
   * The dedupe itself is guarded in `test_test_set_resolver.py`
   * (`test_a_retry_under_the_same_client_token_does_not_publish_twice`); what can only be
   * checked here is that the page sends a token at all, that it is the same one after a
   * failure, and that success retires it so the next publish is a new attempt.
   */
  it('sends a retry-stable client token, and retires it on success', () => {
    expect(DETAIL).toMatch(/clientToken: publishAttemptToken\.current/);
    // Reused while set: only assigned when absent, so a retry carries the first token.
    expect(DETAIL).toMatch(/if \(!publishAttemptToken\.current\)/);
    // Cleared on the success path only — the catch block must not reset it.
    const handler = DETAIL.slice(DETAIL.indexOf('const handlePublishVersion'), DETAIL.indexOf('const handleResetLabels'));
    const [beforeCatch, afterCatch] = handler.split('} catch (err) {');
    expect(beforeCatch).toMatch(/publishAttemptToken\.current = null;/);
    expect(afterCatch).not.toMatch(/publishAttemptToken\.current = null;/);
    // Dismissing abandons the attempt, so the next publish is a new one rather than a replay.
    expect(DETAIL).toMatch(/const dismissPublishDialog = \(\) => \{[\s\S]*?publishAttemptToken\.current = null;/);
    expect(DETAIL).toMatch(/onDismiss=\{dismissPublishDialog\}/);
  });

  /**
   * A retry sends the same token, so it must not send *different* input — and must not
   * report the input rather than the outcome.
   *
   * The dialog resets label, notes and the active-reference choice to their defaults every
   * time it opens. Closing it on failure therefore had two consequences: a retry published
   * with an empty label and the reference checkbox back on, which is not what the user chose;
   * and the toast, built from the request, announced "made it this set's active reference"
   * for a replay of a version published with that box cleared. Neither had happened.
   */
  it('keeps the dialog and its entries on failure, and reports the outcome not the request', () => {
    const handler = DETAIL.slice(DETAIL.indexOf('const handlePublishVersion'), DETAIL.indexOf('const handleResetLabels'));
    const [, afterCatch] = handler.split('} catch (err) {');
    // The failure path leaves the dialog open, so the entries survive for the retry.
    expect(afterCatch).not.toMatch(/setShowPublishModal\(false\)/);
    expect(afterCatch).toMatch(/setPublishError\(/);
    // And the error is shown inside the dialog, since a page alert behind a modal reaches
    // nobody.
    expect(DETAIL).toMatch(/error=\{publishError\}/);
    // The message is derived from the response's own activeReference, not from the request.
    expect(handler).toMatch(/published\.activeReference === published\.version/);
    expect(handler).not.toMatch(/input\.setAsActiveReference\s*\n?\s*\?/);
  });

  it('gives the same account of publishing as the dialog does', () => {
    // Publishing copies the set's labels aside, which is why the guard below cares about
    // the set having settled: a copy taken mid-write freezes a half-written set under a
    // version number. The control and the dialog must not describe that differently.
    expect(DETAIL).toMatch(/copies the labels as they stand/);
    expect(DETAIL).toMatch(/records a settled set of labels/);
  });

  it('will not publish an empty set, one mid-labelling, or one still being written', () => {
    // A version records the labels as they stand, so a set something is still
    // writing to is the moment not to. The server refuses an empty set as well, but
    // checks nothing about the set's status, so that condition lives only here.
    const reason = DETAIL.slice(DETAIL.indexOf('const publishBlockedReason ='), DETAIL.indexOf('const hasConfidence ='));
    expect(reason).toMatch(/labelJob\?\.status === 'RUNNING'/);
    expect(reason).toMatch(/totalCount === 0/);
    expect(reason).toMatch(/setStatus !== 'COMPLETED'/);
    // A set still being copied into has no documents yet, and "still copying" is the
    // more useful of the two true statements, so the status branch comes first.
    expect(reason.indexOf('COMPLETED')).toBeLessThan(reason.indexOf('totalCount === 0'));
  });

  it('gives a reason for every condition that dims the control, including the transient one', () => {
    // `disabledReason` and not a wrapper's `title`: a disabled button is not
    // focusable, so an ancestor tooltip is announced to nobody.
    expect(DETAIL).toMatch(/disabledReason=\{publishBlockedReason \?\? undefined\}/);
    expect(DETAIL).not.toMatch(/<span title=\{publishBlockedReason/);
    // Every branch of the reason is a string, so nothing dims without explanation.
    expect(DETAIL).toMatch(/disabled=\{publishBlockedReason !== null\}/);
    const reason = DETAIL.slice(DETAIL.indexOf('const publishBlockedReason ='), DETAIL.indexOf('const hasConfidence ='));
    expect(reason).toMatch(/isLoading\s*\n?\s*\? 'Loading this test set'/);
    // FAILED is terminal, so it gets its own sentence rather than "wait".
    expect(reason).toMatch(/setStatus === 'FAILED'/);
    expect(reason).toMatch(/nothing settled to record/);
  });

  it('does not read a failed document load as an empty set', () => {
    // `totalCount` stays null when the fetch failed, so `=== 0` alone would leave the
    // control live on a page showing a load error and no documents.
    const reason = DETAIL.slice(DETAIL.indexOf('const publishBlockedReason ='), DETAIL.indexOf('const hasConfidence ='));
    expect(reason).toMatch(/totalCount === null && documents\.length === 0/);
  });

  it('takes the set status off the documents page, not a second query', () => {
    expect(DETAIL).toMatch(/setSetStatus\(page\?\.status \?\? null\)/);
    const docsQuery = readFileSync(join(HERE, '..', '..', 'graphql', 'operations', 'queries', 'GetTestSetDocuments.graphql'), 'utf-8');
    expect(docsQuery).toMatch(/^\s+status$/m);

    // The client guard permits an absent status — it must, since the field is unknown
    // until the first read returns — so it fails open, and the resolver returning the
    // field is what makes it bite. Asserted there, since nothing on this side can:
    // `test_test_set_resolver.py::test_documents_page_carries_the_sets_own_status`.
    const RESOLVER_TESTS = readFileSync(
      join(HERE, '..', '..', '..', '..', '..', 'lib', 'idp_common_pkg', 'tests', 'unit', 'test_test_set_resolver.py'),
      'utf-8',
    );
    expect(RESOLVER_TESTS).toMatch(/def test_documents_page_carries_the_sets_own_status/);
    expect(RESOLVER_TESTS).toMatch(/assert page\["status"\] == "COPYING"/);
  });

  it('reads the existing versions only when the dialog opens', () => {
    // getTestSetVersions is Admin-or-Author and this page is also reachable by an
    // Annotator, so it must not be on the page-load path.
    const open = DETAIL.slice(DETAIL.indexOf('const openPublishDialog = async'), DETAIL.indexOf('const handlePublishVersion = async'));
    expect(open).toMatch(/query: getTestSetVersions/);
    const fetchPage = DETAIL.slice(DETAIL.indexOf('const fetchPage = useCallback('), DETAIL.indexOf('[testSetId],'));
    expect(fetchPage).not.toMatch(/getTestSetVersions/);
  });
});

describe('the permission guard on publishing', () => {
  it('gates the control on the two groups the server declares', () => {
    expect(RBAC).toMatch(/^ {2}publishTestSetVersion:\n {4}groups: \[Admin, Author\]/m);
    // canWrite is that pair, so the control cannot drift wider than the server.
    expect(ROLE).toMatch(/const canWrite = isAdmin \|\| isAuthor;/);
    expect(DETAIL).toMatch(/const \{ isAdmin, canWrite \} = useUserRole\(\);/);
    const header = DETAIL.slice(DETAIL.indexOf('<Header\n                variant="h1"'), DETAIL.indexOf('Test Set: {testSetId}'));
    expect(header).toMatch(/canWrite \? \(/);
    expect(header).toMatch(/onClick=\{openPublishDialog\}/);
  });
});

describe('the test set table', () => {
  it('no longer publishes: one path, on the set itself', () => {
    expect(TEST_SETS).not.toMatch(/id: 'publish'/);
    expect(TEST_SETS).not.toMatch(/const handlePublishVersion = /);
    expect(TEST_SETS).not.toMatch(/publishTestSetVersion/);
  });

  it("still reports each set's version at a glance", () => {
    expect(TEST_SETS).toMatch(/id: 'version',\n {6}header: 'Version'/);
    expect(TEST_SETS).toMatch(/item\.activeReference/);
  });

  it('lost no bulk capability, because the mutation has none', () => {
    // A single String, not a list: the table could only ever publish one set at a
    // time, which is exactly the scope of the page the control moved to.
    const input = SCHEMA.slice(SCHEMA.indexOf('input PublishTestSetVersionInput {'));
    expect(input.slice(0, input.indexOf('}'))).toMatch(/testSetId: String!/);
    expect(SCHEMA).toMatch(/publishTestSetVersion\(input: PublishTestSetVersionInput!\)/);
  });
});
