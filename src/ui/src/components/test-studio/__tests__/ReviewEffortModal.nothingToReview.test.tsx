// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * A set with no draft labels left to review.
 *
 * estimateReviewEffort reports totalDocs 0 when the sampled documents all carry
 * ground truth (#1479). The dialog must then say there is nothing to review and
 * nothing else: a prior-trust banner, three priced strategies and a math section
 * beside that alert would all describe zero documents.
 *
 * The sample is capped and time-bounded, so a zero is a fact about the whole set
 * only when inspectedDocs reaches setDocs. Below that, the dialog must neither say
 * the set is fully labelled nor disable the way to the queue.
 */

import { render, screen, waitFor } from '@testing-library/react';
import React from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const graphql = vi.fn();
vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: (...a: unknown[]) => graphql(...a) }) }));
vi.mock('../../../graphql/generated', () => ({ estimateReviewEffort: 'estimateReviewEffort' }));

// Imported after the mocks, which vitest hoists.
import ReviewEffortModal, { type ReviewEffortEstimate } from '../ReviewEffortModal';

// Typed, so a field added to the estimate's interface fails here at compile time.
const estimate = (totalDocs: number, inspectedDocs: number, setDocs: number): ReviewEffortEstimate => ({
  testSetId: 'docsplit',
  targetAccuracy: 99,
  curveSource: 'prior',
  docsToReview: totalDocs ? 1 : 0,
  docsToReviewLow: 0,
  docsToReviewHigh: totalDocs ? 2 : 0,
  totalDocs,
  sampledDocs: totalDocs,
  inspectedDocs,
  setDocs,
  impliedCutoff: null,
  residualError: 0,
  baselineError: 0,
  effortMinutes: 0,
  effortMinutesPerDoc: 0,
  estimateConfidence: 'prior',
  auditSampleSize: 0,
  recommendReviewAll: false,
  calibration: {
    reliable: false,
    degenerate: false,
    overconfident: false,
    undiscriminating: false,
    totalObservations: 0,
    binCoverage: 0,
    ece: null,
    auroc: null,
  },
  burndown: [],
  reliabilityTable: [],
});

const renderWith = async (totalDocs: number, inspectedDocs = 3, setDocs = 3) => {
  graphql.mockResolvedValue({ data: { estimateReviewEffort: estimate(totalDocs, inspectedDocs, setDocs) } });
  render(<ReviewEffortModal visible testSetId="docsplit" onDismiss={() => {}} onContinue={() => {}} />);
  await waitFor(() => expect(graphql).toHaveBeenCalled());
  // The header gains its " · " detail only once the estimate has arrived.
  await screen.findByText(/^docsplit · /);
};

const continueDisabled = (): boolean => {
  const button = screen.getByRole('button', { name: 'Continue to annotation' }) as HTMLButtonElement;
  return button.disabled || button.getAttribute('aria-disabled') === 'true';
};

describe('ReviewEffortModal with nothing to review', () => {
  beforeEach(() => graphql.mockReset());

  it('shows only the nothing-to-review alert', async () => {
    await renderWith(0);
    expect(screen.getByText('Nothing to review')).toBeTruthy();
    expect(screen.getByText('docsplit · no documents with draft labels')).toBeTruthy();
    expect(screen.queryByText(/choose how much to review/)).toBeNull();
    expect(screen.queryByText(/Rough estimate/)).toBeNull();
    expect(screen.queryByText('How much should annotators review?')).toBeNull();
    expect(screen.queryByText(/Show the math/)).toBeNull();
    expect(continueDisabled()).toBe(true);
  });

  it('does not call a partly sampled set fully labelled, and keeps the queue open', async () => {
    // 200 of 500 read, all ground truth: the estimate is zero, but 300 documents
    // were never read and may hold drafts.
    await renderWith(0, 200, 500);
    expect(screen.queryByText('Nothing to review')).toBeNull();
    expect(screen.getByText('No draft labels in the sample')).toBeTruthy();
    expect(screen.getByText('docsplit · no draft labels among 200 of 500 documents sampled')).toBeTruthy();
    expect(screen.queryByText('How much should annotators review?')).toBeNull();
    expect(continueDisabled()).toBe(false);
  });

  it('still shows the estimate when there is something to review', async () => {
    // The control for the test above: the same fixture with one document renders
    // everything that test asserts is absent, so its absence there is the gate.
    await renderWith(3);
    expect(screen.queryByText('Nothing to review')).toBeNull();
    expect(screen.getByText(/3 document\(s\) with draft labels · choose how much to review/)).toBeTruthy();
    expect(screen.getByText(/Rough estimate/)).toBeTruthy();
    expect(screen.getByText('How much should annotators review?')).toBeTruthy();
    expect(screen.getByText(/Show the math/)).toBeTruthy();
    expect(screen.queryByText('No draft labels in the sample')).toBeNull();
    expect(continueDisabled()).toBe(false);
  });
});
