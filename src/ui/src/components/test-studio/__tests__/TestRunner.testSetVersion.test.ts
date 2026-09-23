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

import { testSetVersionOptions, CURRENT_LABELS, NO_STORED_LABELS } from '../testSetVersionOptions';

const HERE = join(__dirname, '..');
const RUNNER = readFileSync(join(HERE, 'TestRunner.tsx'), 'utf-8');
const RESULTS = readFileSync(join(HERE, 'TestResultsList.tsx'), 'utf-8');
const SCHEMA = readFileSync(join(HERE, '..', '..', '..', '..', '..', 'nested', 'api-resolvers', 'src', 'api', 'schema.graphql'), 'utf-8');
const OPS = join(HERE, '..', '..', 'graphql', 'operations');

describe('the run form', () => {
  it('offers a test set version picker that defaults to current labels', () => {
    expect(RUNNER).toMatch(/label="Test set version"/);
    // The option itself is built in testSetVersionOptions.ts and asserted there, against
    // real inputs rather than the component's text.
    expect(testSetVersionOptions([])[0].label).toBe('Current labels');
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

  it('builds its options from the shared rule rather than inline', () => {
    // So the rule below is the rule the form uses.
    expect(RUNNER).toMatch(/testSetVersionOptions\(testSetVersions\)/);
  });

  it('and the query fetches the field the rule reads', () => {
    const text = readFileSync(join(OPS, 'queries/GetTestSetVersions.graphql'), 'utf-8');
    expect(text).toMatch(/^\s*hasStoredLabels$/m);
  });
});

/**
 * Which versions the picker warns about.
 *
 * A version with no labels stored under `versions/{n}/baseline/` is staged from the set's
 * **current** labels instead — `test_file_copier._resolve_baseline_folder` falls back when
 * the prefix is empty — so pinning it does not do what pinning is for. The fallback is
 * otherwise visible only in the copier's log after the run, so the option says it.
 *
 * Asserted against real inputs rather than the component's source, because the interesting
 * part is *which* versions get the warning, and a regex matching the condition would pass
 * with the branches inverted. The backend half is guarded where it lives:
 * `test_test_set_resolver.py` for `hasStoredLabels`, and
 * `test_file_copier_baseline_version.py` for the fallback itself.
 */
describe('the version picker rule', () => {
  it('warns when the version has no stored labels, whatever its object count says', () => {
    const [, published, backfilled] = testSetVersionOptions([
      // Published from a set with no labels yet: a count of 0, and nothing stored.
      { version: 2, label: 'first pass', snapshotObjectCount: 0, hasStoredLabels: false },
      // Published before publishing copied anything, then backfilled when annotation
      // opened: no count, and labels stored all the same. This one must NOT warn.
      { version: 1, label: 'v1', snapshotObjectCount: null, hasStoredLabels: true },
    ]);

    expect(published.description).toBe(`first pass · ${NO_STORED_LABELS}`);
    expect(backfilled.description).toBe('v1');
  });

  it('says nothing when the version does have stored labels', () => {
    const [, option] = testSetVersionOptions([{ version: 3, label: 'reviewed', snapshotObjectCount: 12, hasStoredLabels: true }]);

    expect(option.description).toBe('reviewed');
  });

  it('claims nothing when the backend did not say', () => {
    // An older backend does not return the field. Absent is not false.
    const [, option] = testSetVersionOptions([{ version: 1, label: 'v1' }]);

    expect(option.description).toBe('v1');
  });

  it('still warns on an unlabelled version, without a leading separator', () => {
    const [, option] = testSetVersionOptions([{ version: 1, hasStoredLabels: false }]);

    expect(option.description).toBe(NO_STORED_LABELS);
  });

  it('leads with the current labels, which is the default', () => {
    expect(testSetVersionOptions([])[0]).toMatchObject({ value: CURRENT_LABELS, label: 'Current labels' });
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
