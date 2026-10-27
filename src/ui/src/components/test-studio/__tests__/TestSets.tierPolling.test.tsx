// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * How often the Test Sets list asks for each set's quality tier.
 *
 * The tier comes from estimateReviewEffort, which reads the set's metadata, its
 * stored calibration curve and up to a couple of hundred documents' confidences,
 * so it is meant to be requested once per label state and never per poll. The
 * list itself re-fetches every 60 seconds (and every 3 seconds while a set is in
 * flux), and getTestSets does not return its sets in a stable order: the resolver
 * reads the records with BatchGetItem, which returns them in no particular order,
 * and never sorts. A tier request keyed on the list's order therefore went out for
 * every labeled set on any poll that came back reordered.
 *
 * The fixtures are typed with the generated operation types, which codegen derives
 * from the API schema, so a field the resolver does not send cannot be added here
 * without failing the type check.
 */

import { act, render, screen } from '@testing-library/react';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { EstimateReviewEffortQuery, GetTestSetsQuery } from '../../../graphql/generated/operation-types';

const graphql = vi.fn();
vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: (...a: unknown[]) => graphql(...a) }) }));
vi.mock('../../../graphql/generated', () => ({
  deleteTestSets: 'deleteTestSets',
  getTestSets: 'getTestSets',
  estimateReviewEffort: 'estimateReviewEffort',
  getDraftLabelJob: 'getDraftLabelJob',
  updateTestSet: 'updateTestSet',
}));
vi.mock('../../../hooks/use-user-role', () => ({ default: () => ({ isAdmin: false }) }));
const noJob = async () => null;
const noJobs = async () => [];
vi.mock('../../../hooks/use-synthetic-data-generator', () => ({
  default: () => ({ available: false, getJobStatus: noJob, listActiveJobs: noJobs }),
}));
vi.mock('../GenerateSyntheticDataModal', () => ({ default: () => null }));
vi.mock('../CreateTestSetWizard', () => ({ default: () => null }));
vi.mock('../AddDocumentsModals', () => ({ default: () => null }));
vi.mock('../TestSetDetail', () => ({
  LabelAccuracyLegend: () => null,
  renderLabelAccuracy: (entry?: { tier?: string | null } | null) => (entry?.tier ? `tier:${entry.tier}` : 'no tier'),
}));

// Imported after the mocks, which vitest hoists.
import TestSets from '../TestSets';

type TestSetRow = NonNullable<NonNullable<GetTestSetsQuery['getTestSets']>[number]>;
type Estimate = NonNullable<EstimateReviewEffortQuery['estimateReviewEffort']>;

/** One row as get_test_sets builds it for a registered set. */
const row = (id: string, fields: Partial<TestSetRow> = {}): TestSetRow => ({
  id,
  name: id,
  description: '',
  filePattern: '',
  fileCount: 4,
  source: 'uploaded',
  latestVersion: 1,
  activeReference: 1,
  labelState: 'labeled',
  labelJobId: null,
  labelJobStatus: null,
  status: 'COMPLETED',
  createdAt: `2026-10-0${id.length}T00:00:00+00:00`,
  updatedAt: null,
  error: null,
  documentClassType: null,
  configVersion: null,
  reviewedDocs: null,
  claimLeaseMinutes: 30,
  queueCountsAt: null,
  summaryStatus: 'READY',
  ...fields,
});

const estimate = (testSetId: string): Estimate => ({
  testSetId,
  qualityTier: 'GOLD',
  qualityTierReason: 'measured',
  targetAccuracy: 99,
  configVersion: null,
  configVersionSource: null,
  confidenceFingerprint: null,
  confidenceFingerprintSource: null,
  curveSource: null,
  docsToReview: 1,
  docsToReviewLow: 0,
  docsToReviewHigh: 2,
  totalDocs: 4,
  sampledDocs: 4,
  inspectedDocs: 4,
  setDocs: 4,
  wholeSetInspected: true,
  impliedCutoff: null,
  residualError: 0.01,
  baselineError: 0.02,
  effortMinutes: 5,
  effortMinutesPerDoc: 5,
  estimateConfidence: 'HIGH',
  auditSampleSize: 1,
  recommendReviewAll: false,
  calibration: {
    ece: null,
    auroc: null,
    binCoverage: 1,
    totalObservations: 10,
    degenerate: false,
    overconfident: false,
    undiscriminating: false,
    reliable: true,
  },
  burndown: [],
  reliabilityTable: [],
});

const DISCOVERY_POLL_MS = 60_000;

/**
 * Serve getTestSets from a queue of listings, repeating the last one, and count
 * the estimate requests per set.
 */
const serve = (
  listings: TestSetRow[][],
  respond: (id: string, nth: number) => Promise<unknown> = (id) => Promise.resolve({ data: { estimateReviewEffort: estimate(id) } }),
): Record<string, number> => {
  const estimates: Record<string, number> = {};
  let call = 0;
  graphql.mockImplementation((args: { query: string; variables?: { testSetId?: string } }) => {
    if (args.query === 'getTestSets') {
      const listing = listings[Math.min(call, listings.length - 1)];
      call += 1;
      return Promise.resolve({ data: { getTestSets: listing } });
    }
    if (args.query === 'estimateReviewEffort') {
      const id = args.variables?.testSetId as string;
      estimates[id] = (estimates[id] ?? 0) + 1;
      return respond(id, estimates[id]);
    }
    return Promise.resolve({ data: {} });
  });
  return estimates;
};

/**
 * Run the discovery poll `polls` times, one act() per tick. Inside a single act()
 * React applies every tick's state update before rendering, so two polls there
 * render once and a reordering between them is never seen by the component.
 */
const poll = async (polls: number): Promise<void> => {
  for (let i = 0; i < polls; i += 1) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(DISCOVERY_POLL_MS);
    });
  }
};

const mountAndPoll = async (polls: number): Promise<void> => {
  render(
    <MemoryRouter>
      <TestSets />
    </MemoryRouter>,
  );
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0);
  });
  await poll(polls);
};

describe('the quality-tier requests on the Test Sets list', () => {
  beforeEach(() => {
    graphql.mockReset();
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('requests each labeled set once across polls that return the same sets reordered', async () => {
    const a = row('set-a');
    const b = row('set-bb');
    const c = row('set-ccc');
    const estimates = serve([
      [a, b, c],
      [c, a, b],
      [b, c, a],
      [a, c, b],
    ]);

    await mountAndPoll(4);

    // Four polls ran after the first load; each set was still estimated once.
    expect(graphql.mock.calls.filter(([args]) => args.query === 'getTestSets')).toHaveLength(5);
    expect(estimates).toEqual({ 'set-a': 1, 'set-bb': 1, 'set-ccc': 1 });
    expect(screen.getAllByText('tier:GOLD')).toHaveLength(3);
  });

  it('re-rates only the set whose label state changed', async () => {
    const a = row('set-a');
    const b = row('set-bb', { labelJobId: 'job-1', labelJobStatus: 'RUNNING', labelState: 'draft' });
    const bDone = row('set-bb', { labelJobId: 'job-1', labelJobStatus: 'COMPLETED', labelState: 'draft' });
    // The change arrives in the same order as the poll before it, so only the
    // label state, not a reordering, can be what triggers the second request.
    const estimates = serve([
      [a, b],
      [b, a],
      [bDone, a],
    ]);

    await mountAndPoll(3);

    expect(estimates).toEqual({ 'set-a': 1, 'set-bb': 2 });
  });

  it('rates a newly listed set without re-rating the others', async () => {
    const a = row('set-a');
    const b = row('set-bb');
    const c = row('set-ccc');
    const estimates = serve([
      [a, b],
      [c, b, a],
      [a, b, c],
    ]);

    await mountAndPoll(2);

    expect(estimates).toEqual({ 'set-a': 1, 'set-bb': 1, 'set-ccc': 1 });
  });

  it('never requests a tier for an unlabeled set, and rates it once it is labeled', async () => {
    const a = row('set-a');
    const u = row('set-bb', { labelState: 'unlabeled' });
    const uLabeled = row('set-bb', { labelState: 'labeled' });
    const estimates = serve([
      [a, u],
      [u, a],
      [uLabeled, a],
    ]);

    // The first load and one reordered poll, both with the set unlabeled.
    await mountAndPoll(1);
    expect(estimates).toEqual({ 'set-a': 1 });

    await poll(2);
    expect(estimates).toEqual({ 'set-a': 1, 'set-bb': 1 });
  });

  it('rates a set again when it leaves the list and comes back', async () => {
    const a = row('set-a');
    const b = row('set-bb');
    const estimates = serve([[a, b], [b], [b, a]]);

    await mountAndPoll(2);

    expect(estimates).toEqual({ 'set-a': 2, 'set-bb': 1 });
  });

  it('retries a failed estimate when the list next changes, and only that one', async () => {
    const a = row('set-a');
    const b = row('set-bb');
    const estimates = serve(
      [
        [a, b],
        [b, a],
        [b, a],
        [b, a],
      ],
      (id, nth) =>
        id === 'set-a' && nth === 1
          ? Promise.reject(new Error('timed out'))
          : Promise.resolve({ data: { estimateReviewEffort: estimate(id) } }),
    );

    await mountAndPoll(3);

    expect(estimates).toEqual({ 'set-a': 2, 'set-bb': 1 });
    expect(screen.getAllByText('tier:GOLD')).toHaveLength(2);
  });

  it('keeps a newer request when an older one for the same set fails', async () => {
    const running = row('set-a', { labelJobId: 'job-1', labelJobStatus: 'RUNNING', labelState: 'draft' });
    const done = row('set-a', { labelJobId: 'job-1', labelJobStatus: 'COMPLETED', labelState: 'draft' });
    const b = row('set-bb');
    let failFirst: (err: Error) => void = () => undefined;
    const estimates = serve(
      [
        [running, b],
        [done, b],
        [b, done],
        [done, b],
      ],
      (id, nth) => {
        if (id === 'set-a' && nth === 1) {
          return new Promise((_resolve, reject) => {
            failFirst = reject;
          });
        }
        return Promise.resolve({ data: { estimateReviewEffort: estimate(id) } });
      },
    );

    // The request for the running job is still pending when the job finishes.
    await mountAndPoll(1);
    expect(estimates).toEqual({ 'set-a': 2, 'set-bb': 1 });

    // The stale request fails after the newer one was made; the reordered polls
    // that follow must not request the finished set again.
    await act(async () => {
      failFirst(new Error('timed out'));
      await vi.advanceTimersByTimeAsync(0);
    });
    await poll(2);
    expect(estimates).toEqual({ 'set-a': 2, 'set-bb': 1 });
  });
});
