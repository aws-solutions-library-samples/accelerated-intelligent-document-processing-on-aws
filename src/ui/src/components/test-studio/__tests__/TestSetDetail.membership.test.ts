// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Editing a test set's membership from its own page.
 *
 * The remove mutation had existed since the versioning work with nothing calling it,
 * and the Add documents dialogs were reachable only from the table page. This pins
 * the wiring that makes both usable where a user pruning or growing a set actually
 * stands, and the guards that keep an empty set from being run or published.
 *
 * Asserted at source level, following the sibling tests in this directory: the page
 * needs the GraphQL client, settings, role and generator hooks to render.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

const HERE = join(__dirname, '..');
const ROOT = join(HERE, '..', '..', '..', '..', '..');
const DETAIL = readFileSync(join(HERE, 'TestSetDetail.tsx'), 'utf-8');
const TEST_SETS = readFileSync(join(HERE, 'TestSets.tsx'), 'utf-8');
const RUNNER = readFileSync(join(HERE, 'TestRunner.tsx'), 'utf-8');
const ALIASES = readFileSync(join(ROOT, 'nested', 'api-resolvers', 'src', 'lambda', 'http_api_dispatcher', 'index.py'), 'utf-8');
const RBAC = readFileSync(join(ROOT, 'scripts', 'api_rbac_expectations.yaml'), 'utf-8');

describe('the set detail page', () => {
  it('lets rows be selected and removes them by their name under input/', () => {
    expect(DETAIL).toMatch(/selectionType="multi"/);
    // objectKey is the relative name the mutation deletes under; inputKey is the
    // full object key and would match nothing.
    expect(DETAIL).toMatch(/fileNames: selectedItems\.map\(\(d\) => d\.objectKey\)/);
    expect(DETAIL).not.toMatch(/fileNames: selectedItems\.map\(\(d\) => d\.inputKey\)/);
  });

  it('disables Remove without a selection and while draft labeling runs', () => {
    const remove = DETAIL.slice(
      DETAIL.indexOf('onClick={() => setShowRemoveModal(true)}'),
      DETAIL.indexOf('Remove\n', DETAIL.indexOf('onClick={() => setShowRemoveModal(true)}')),
    );
    expect(remove).toMatch(/disabled=\{selectedItems\.length === 0 \|\| isLoading \|\| labelJob\?\.status === 'RUNNING'\}/);
  });

  it('offers all three add sources, gating generation on the extension', () => {
    expect(DETAIL).toMatch(/text: 'From files in a bucket'/);
    expect(DETAIL).toMatch(/text: 'From a zip upload'/);
    const generate = DETAIL.slice(
      DETAIL.indexOf("id: 'add-generate'"),
      DETAIL.indexOf('onItemClick', DETAIL.indexOf("id: 'add-generate'")),
    );
    expect(generate).toMatch(/disabled: !generatorAvailable/);
    expect(DETAIL).toMatch(/initialDestination=\{\{ testSetId, label: testSetId \}\}/);
  });

  it('will not offer draft labeling on an empty set', () => {
    expect(DETAIL).toMatch(/disabled=\{isLoading \|\| labelJob\?\.status === 'RUNNING' \|\| totalCount === 0\}/);
  });

  it('shows one membership notice at a time', () => {
    // Seen live: after removing the documents an earlier add had brought in, the
    // green "Documents are arriving" notice stayed beside "Removed 2 document(s)".
    const remove = DETAIL.slice(DETAIL.indexOf('const handleRemoveDocuments = async'), DETAIL.indexOf('const handleResetLabels = async'));
    expect(remove).toMatch(/stopArrivalWatch\(\);\s*setArrivalNotice\(null\);/);
    const arrival = DETAIL.slice(DETAIL.indexOf('const watchForArrival = '), DETAIL.indexOf('const handleRemoveDocuments = async'));
    expect(arrival.match(/setRemovedMessage\(null\)/g)).toHaveLength(2);
  });

  it('clears the selection whenever the page is refetched', () => {
    const fetchPage = DETAIL.slice(
      DETAIL.indexOf('const fetchPage = useCallback('),
      DETAIL.indexOf('[testSetId],', DETAIL.indexOf('const fetchPage = useCallback(')),
    );
    expect(fetchPage).toMatch(/setSelectedItems\(\[\]\)/);
  });
});

describe('the shared Add documents dialogs', () => {
  it('are the only copy: the table page no longer carries its own', () => {
    expect(TEST_SETS).toMatch(/<AddDocumentsModals/);
    expect(TEST_SETS).not.toMatch(/const handleAddDocuments = /);
    expect(TEST_SETS).not.toMatch(/const handleCheckFiles = /);
    expect(TEST_SETS).not.toMatch(/showAddDocsPatternModal/);
    expect(DETAIL).toMatch(/<AddDocumentsModals/);
  });
});

describe('importing by file pattern', () => {
  // Matching a pattern searches a whole bucket, so it is Admin-only end to end:
  // the resolver refuses Authors, and no surface offers them the source.
  const WIZARD = readFileSync(join(HERE, 'CreateTestSetWizard.tsx'), 'utf-8');
  const RESOLVER = readFileSync(join(ROOT, 'nested', 'api-resolvers', 'src', 'lambda', 'test_set_resolver', 'index.py'), 'utf-8');
  const SCHEMA = readFileSync(join(ROOT, 'nested', 'api-resolvers', 'src', 'api', 'schema.graphql'), 'utf-8');

  it('is refused server-side for anyone but an Admin', () => {
    const adminOnly = RESOLVER.slice(
      RESOLVER.indexOf('ADMIN_ONLY_FIELDS = ('),
      RESOLVER.indexOf(')', RESOLVER.indexOf('ADMIN_ONLY_FIELDS = (')),
    );
    for (const op of ['listBucketFiles', 'addTestSet', 'addDocumentsToTestSet']) {
      expect(adminOnly, op).toContain(`"${op}"`);
      expect(RBAC, op).toMatch(new RegExp(`^ {2}${op}:\n {4}groups: \\[Admin\\]`, 'm'));
      const decl = SCHEMA.slice(SCHEMA.indexOf(`\n  ${op}(`));
      expect(decl.slice(0, decl.indexOf(')\n', decl.indexOf('@aws_cognito')) + 2), op).toMatch(/cognito_groups: \["Admin"\]/);
    }
  });

  it('is not offered to Authors on any of the three surfaces', () => {
    expect(WIZARD).toMatch(/s\.value !== 'existing-files' \|\| isAdmin/);
    expect(WIZARD).toMatch(/Importing by file pattern from a bucket is available to administrators\./);
    expect(TEST_SETS).toMatch(/id: 'docs-pattern', text: 'From files in a bucket', disabled: !isAdmin/);
    expect(DETAIL).toMatch(/id: 'add-pattern', text: 'From files in a bucket', disabled: !isAdmin/);
  });
});

describe('an empty set', () => {
  it('cannot be run from the run form', () => {
    expect(RUNNER).toMatch(/selectedFileCount === 0 \? 'This test set has no documents'/);
    expect(RUNNER).toMatch(/disabled=\{Boolean\(runDisabledReason\)\}/);
  });

  it('cannot be published or annotated from the table', () => {
    const publish = TEST_SETS.slice(TEST_SETS.indexOf("id: 'publish'"), TEST_SETS.indexOf('},', TEST_SETS.indexOf("id: 'publish'")));
    expect(publish).toMatch(/!selectedItems\[0\]\?\.fileCount/);
    expect(TEST_SETS).toMatch(
      /id: 'annotate', text: 'Annotate ground truth', disabled: selectedItems\.length !== 1 \|\| !selectedItems\[0\]\?\.fileCount/,
    );
  });

  it('is created through an operation the API layer knows about', () => {
    // A field the dispatcher cannot route 404s silently, and one missing from the
    // RBAC manifest fails the static authorization scan.
    expect(ALIASES).toMatch(/"createEmptyTestSet": "addDocumentsToTestSet"/);
    expect(RBAC).toMatch(/^ {2}createEmptyTestSet:\n {4}groups: \[Admin, Author\]/m);
  });
});
