// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The Test Studio queue page as an Annotator works it: three document filters (opening
 * on Not assigned), the
 * progress bar on its own, every key metric in one grid, and claiming several
 * documents at once. Checked against the component source, as the neighbouring
 * AnnotationWorkspace suites are.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

const SOURCE = readFileSync(join(__dirname, '..', 'AnnotationWorkspace.tsx'), 'utf-8');

const between = (start: string, end: string): string => {
  const from = SOURCE.indexOf(start);
  expect(from, `missing ${start}`).toBeGreaterThanOrEqual(0);
  return SOURCE.slice(from, SOURCE.indexOf(end, from));
};

describe('document filters', () => {
  it('offers exactly All documents, Assigned and Not assigned, in that order, opening on Not assigned', () => {
    const options = between('const STATUS_FILTER_OPTIONS', '];');
    const labels = [...options.matchAll(/label: '([^']+)'/g)].map((m) => m[1]);
    expect(labels).toEqual(['All documents', 'Assigned', 'Not assigned']);
    expect(SOURCE).toMatch(/const DEFAULT_STATUS_FILTER: DocStatusFilter = 'unassigned';/);
  });

  it('makes Not assigned the documents an Annotator can claim', () => {
    // The same predicate as isClaimable, so the filter never lists a row whose Claim is
    // disabled, nor hides one that could be claimed.
    expect(SOURCE).toMatch(/case 'unassigned':\s*return !isDone\(d\) && !d\.assignedTo;/);
    expect(SOURCE).toMatch(/const isClaimable = \(item: QueueItem\): boolean => !item\.assignedTo && !isDone\(item\);/);
  });
});

describe('queue progress and key metrics', () => {
  const progress = between('<Header variant="h2">Queue Progress</Header>', '</Container>');
  const metrics = between('<Header variant="h2">Key Metrics</Header>', '</Container>');

  it('shows the progress bar on its own, taller than the default', () => {
    expect(progress).toContain('<ProgressBar');
    expect(progress).toMatch(/style=\{\{ progressBar: \{ height: '16px'/);
    expect(progress).not.toContain('metric(');
  });

  it('lays every metric out in one grid, so later rows keep the first row’s columns', () => {
    const grids = metrics.match(/<ColumnLayout /g) ?? [];
    expect(grids).toHaveLength(1);
    for (const label of ['Status', 'Pending', 'Assigned', 'Annotators', 'Estimated label accuracy', 'Evidence']) {
      expect(metrics).toContain(`'${label}'`);
    }
    expect(metrics).not.toContain(`'Quality'`);
  });
});

describe('claiming and assigning several documents at once', () => {
  it('lets an Annotator select only documents nobody holds', () => {
    expect(SOURCE).toMatch(/selectionType=\{isAdmin \|\| claimsWork \? 'multi' : undefined\}/);
    expect(SOURCE).toMatch(/isItemDisabled=\{isAdmin \? undefined : \(item\) => !isClaimable\(item\)\}/);
  });

  it('claims each selected document separately and reports the ones someone else took', () => {
    const handler = between('const handleBulkClaim = async', 'const openSettings');
    expect(handler).toMatch(/for \(const key of keys\) \{\s*try \{\s*await claimTestSetDocument\(testSetId, key\);/);
    expect(handler).toContain('failed.push(key)');
    expect(handler).toContain('another Annotator took');
    expect(SOURCE).toMatch(/onClick=\{\(\) => void handleBulkClaim\(\)\}/);
  });

  it('still lets an Admin assign several selected documents to one Annotator', () => {
    expect(SOURCE).toMatch(/`Assign to… \(\$\{assignable\.length\}\)`/);
  });
});
