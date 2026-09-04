// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Which labels a test run scores against, and whether the screen says so.
 *
 * Found in review: once a version transition had snapshotted `versions/1/baseline/`,
 * every run pinned to `activeReference` and so scored the labels from BEFORE the
 * corrections — silently, with the run form offering no control and the results table
 * no indication. The run form already had the answer for configuration: a revision
 * picker that defaults to current and pins explicitly. The test set now gets the same.
 *
 * Asserted at source level, following the sibling tests in this directory.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

const HERE = join(__dirname, '..');
const RUNNER = readFileSync(join(HERE, 'TestRunner.tsx'), 'utf-8');
const RESULTS = readFileSync(join(HERE, 'TestResultsList.tsx'), 'utf-8');
const SCHEMA = readFileSync(join(HERE, '..', '..', '..', '..', '..', 'nested', 'api-resolvers', 'src', 'api', 'schema.graphql'), 'utf-8');
const OPS = join(HERE, '..', '..', 'graphql', 'operations');

describe('the run form', () => {
  it('offers a test set version picker that defaults to current labels', () => {
    expect(RUNNER).toMatch(/label="Test set version"/);
    expect(RUNNER).toMatch(/label: 'Current labels'/);
  });

  it('sends the version only when one was pinned', () => {
    // Absent means current labels on the server; sending null would be the same
    // thing said two ways, and sending 0 would be a version that does not exist.
    expect(RUNNER).toMatch(/\.\.\.\(selectedTestSetVersion !== null && \{ testSetVersion: selectedTestSetVersion \}\)/);
  });

  it('resets the pin when the set changes, since versions belong to a set', () => {
    const effect = RUNNER.slice(RUNNER.indexOf('setSelectedTestSetVersion(null);'), RUNNER.indexOf('}, [selectedTestSet?.value]);'));
    expect(effect).toMatch(/getTestSetVersions/);
  });

  it('describes the default and the reason to pin, as the config picker does', () => {
    expect(RUNNER).toMatch(/Defaults to the set’s current labels, including any annotation in progress/);
  });
});

describe('the results table', () => {
  it('shows what each run scored against beside the set', () => {
    expect(RESULTS).toMatch(/<Badge color="grey">v\{item\.testSetVersion\}<\/Badge>/);
    expect(RESULTS).toMatch(/current &rarr; v\{item\.testSetDraftVersion\}/);
  });

  it('and the queries actually fetch those fields', () => {
    for (const op of ['queries/GetTestRuns.graphql', 'queries/GetTestRun.graphql']) {
      const text = readFileSync(join(OPS, op), 'utf-8');
      expect(text, op).toMatch(/^\s*testSetVersion$/m);
      expect(text, op).toMatch(/^\s*testSetDraftVersion$/m);
    }
  });
});

describe('the contract', () => {
  it('is declared on the input and the result', () => {
    const input = SCHEMA.slice(SCHEMA.indexOf('input TestRunInput'), SCHEMA.indexOf('}', SCHEMA.indexOf('input TestRunInput')));
    expect(input).toMatch(/^\s*testSetVersion: Int$/m);
    expect(SCHEMA).toMatch(/^\s*testSetDraftVersion: Int$/m);
  });
});
