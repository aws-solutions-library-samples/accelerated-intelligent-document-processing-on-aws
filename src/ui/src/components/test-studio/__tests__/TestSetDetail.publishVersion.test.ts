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

  it('does not tell the user the active reference decides what runs score against', () => {
    // It does not: `test_runner` never reads `activeReference`, and the runner's
    // version picker defaults to the set's current labels. Saying otherwise at the
    // moment someone decides whether to move the pointer is the worst place to be
    // wrong, so the claim is pinned out of the dialog and the docs.
    const MODAL = readFileSync(join(HERE, 'PublishVersionModal.tsx'), 'utf-8');
    const RUNNER = readFileSync(join(HERE, 'TestRunner.tsx'), 'utf-8');
    const DOC = readFileSync(join(HERE, '..', '..', '..', '..', '..', 'docs', 'test-studio.md'), 'utf-8');
    expect(MODAL).toMatch(/does not decide what a test run is scored against/);
    expect(MODAL).not.toMatch(/active reference is the baseline/);
    expect(DOC).toMatch(/active reference does not decide what a test run is scored against/);

    // The claim's truth condition: the runner does not consult the pointer at all.
    expect(RUNNER).not.toMatch(/activeReference/);
  });

  it('will not publish an empty set, one mid-labelling, or one still being written', () => {
    // A version records the labels as they stand, so a set something is still
    // writing to is the moment not to. The server refuses an empty set as well, but
    // checks nothing about the set's status, so that condition lives only here.
    const reason = DETAIL.slice(DETAIL.indexOf('const publishBlockedReason ='), DETAIL.indexOf('const hasConfidence ='));
    expect(reason).toMatch(/labelJob\?\.status === 'RUNNING'/);
    expect(reason).toMatch(/totalCount === 0/);
    expect(reason).toMatch(/setStatus !== 'COMPLETED'/);
    expect(DETAIL).toMatch(/disabled=\{isLoading \|\| publishBlockedReason !== null\}/);
  });

  it('states the reason rather than only disabling', () => {
    expect(DETAIL).toMatch(/title=\{publishBlockedReason \?\? undefined\}/);
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
