// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * "Edit ground truth" means the same thing on the Test Sets list as on the test set's
 * page: it opens the review-effort dialog for the set, which continues to its queue,
 * or, for a fully labelled set, to the queue with its reviewed documents shown.
 */

import { act, render, screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const graphql = vi.fn();
let effortProps: {
  visible: boolean;
  testSetId: string;
  onContinue: () => void;
  onReviewDocuments: () => void;
} | null = null;
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
vi.mock('../TestSetDetail', () => ({ LabelAccuracyLegend: () => null, renderLabelAccuracy: () => 'no tier' }));
vi.mock('../ReviewEffortModal', () => ({
  default: (props: NonNullable<typeof effortProps>) => {
    effortProps = props;
    return null;
  },
}));

// Imported after the mocks, which vitest hoists.
import TestSets from '../TestSets';

const ROW = {
  id: 'ts1',
  name: 'Invoices',
  description: '',
  filePattern: '',
  fileCount: 4,
  source: 'uploaded',
  latestVersion: 1,
  activeReference: 1,
  labelState: 'labeled',
  status: 'COMPLETED',
  createdAt: '2026-10-01T00:00:00+00:00',
  summaryStatus: 'READY',
};

beforeEach(() => {
  document.body.innerHTML = '';
  effortProps = null;
  window.location.hash = '';
  graphql
    .mockReset()
    .mockImplementation((args: { query: string }) => Promise.resolve({ data: args.query === 'getTestSets' ? { getTestSets: [ROW] } : {} }));
});

const chooseEditGroundTruth = async () => {
  render(
    <MemoryRouter>
      <TestSets />
    </MemoryRouter>,
  );
  await screen.findByText('Invoices');
  const table = createWrapper(document.body).findTable()!;
  act(() => table.findRowSelectionArea(1)!.click());
  const actions = createWrapper(document.body).findButtonDropdown()!;
  act(() => actions.openDropdown());
  act(() => actions.findItemById('annotate')!.click());
};

describe('Edit ground truth on the Test Sets list', () => {
  it('opens the review-effort dialog for the selected set, as the test set page does', async () => {
    await chooseEditGroundTruth();
    await waitFor(() => expect(effortProps).toMatchObject({ visible: true, testSetId: 'ts1' }));
    // Not straight to the queue.
    expect(window.location.hash).toBe('');
  });

  it('continues to the queue, or to its reviewed documents for a fully labelled set', async () => {
    await chooseEditGroundTruth();
    await waitFor(() => expect(effortProps?.visible).toBe(true));
    act(() => effortProps!.onReviewDocuments());
    expect(window.location.hash).toBe('#/review-queues/test-sets/ts1?show=reviewed');
    await waitFor(() => expect(effortProps?.visible).toBe(false));
  });
});
