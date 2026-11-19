// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Deleting several test sets where the server refuses or fails some of them.
 *
 * deleteTestSets deletes every set it can and then rejects, naming only the sets
 * it refused (a job is writing them) or could not finish. So a rejection does not
 * mean nothing was deleted: the page must reload the list rather than keep the
 * deleted sets on screen until a poll, which only runs while a set is in progress,
 * and must say how many went alongside the server's reason for the rest.
 *
 * The rejection is shaped as the REST client throws it (`{ errors: [...] }`), and
 * the reason text is the resolver's own wording for a busy set.
 */

import { act, render, screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import React from 'react';
import { IntlProvider } from 'react-intl';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

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
vi.mock('../TestSetDetail', () => ({ LabelAccuracyLegend: () => null, renderLabelAccuracy: () => 'no tier' }));
vi.mock('../ReviewEffortModal', () => ({ default: () => null }));

// Imported after the mocks, which vitest hoists.
import TestSets from '../TestSets';

const row = (id: string, name: string, createdAt: string) => ({
  id,
  name,
  description: '',
  filePattern: '',
  fileCount: 4,
  source: 'uploaded',
  latestVersion: 1,
  activeReference: 1,
  labelState: 'labeled',
  status: 'COMPLETED',
  createdAt,
  summaryStatus: 'READY',
});

const INVOICES = row('invoices', 'Invoices', '2026-10-01T00:00:00+00:00');
const RECEIPTS = row('receipts', 'Receipts', '2026-10-02T00:00:00+00:00');
const BUSY = "Test set 'receipts' is busy (COPYING); try again when it has finished";

/**
 * Serve the list from a queue, repeating the last listing (`null` makes that load
 * fail), and answer each delete from `deletes` in turn: a string rejects with it.
 */
const serve = (listings: (unknown[] | null)[], ...deletes: (string | true)[]): void => {
  let call = 0;
  let deleteCall = 0;
  graphql.mockImplementation((args: { query: string }) => {
    if (args.query === 'getTestSets') {
      const listing = listings[Math.min(call, listings.length - 1)];
      call += 1;
      if (listing === null) return Promise.reject({ errors: [{ message: 'list unavailable', errorType: 'HttpError' }] });
      return Promise.resolve({ data: { getTestSets: listing } });
    }
    if (args.query === 'deleteTestSets') {
      const outcome = deletes[Math.min(deleteCall, deletes.length - 1)];
      deleteCall += 1;
      if (outcome === true) return Promise.resolve({ data: { deleteTestSets: true } });
      return Promise.reject({ errors: [{ message: outcome, errorType: 'BadRequest' }] });
    }
    return Promise.resolve({ data: {} });
  });
};

const renderPage = async (): Promise<void> => {
  render(
    <IntlProvider locale="en" defaultLocale="en">
      <MemoryRouter>
        <TestSets />
      </MemoryRouter>
    </IntlProvider>,
  );
  await screen.findByText('Invoices');
};

/** Select every listed set (or the row at `rowNumber`, 1-based) and confirm Delete. */
const deleteSelected = (rowNumber?: number): void => {
  const table = createWrapper(document.body).findTable()!;
  act(() => (rowNumber ? table.findRowSelectionArea(rowNumber)! : table.findSelectAllTrigger()!).click());
  const actions = createWrapper(document.body).findButtonDropdown()!;
  act(() => actions.openDropdown());
  act(() => actions.findItemById('delete')!.click());
  const confirm = screen.getAllByRole('button', { name: 'Delete' }).at(-1)!;
  act(() => confirm.click());
};

const deleteBoth = async (): Promise<void> => {
  await renderPage();
  deleteSelected();
};

const listedNames = (): string[] =>
  createWrapper(document.body)
    .findTable()!
    .findRows()
    .map((r) => r.getElement().textContent ?? '');

describe('a delete the server partly refused', () => {
  beforeEach(() => {
    document.body.innerHTML = '';
    graphql.mockReset();
  });

  it('drops the deleted set from the list at once and says how many went', async () => {
    // First load lists both; after the delete the server lists only the busy one.
    serve([[INVOICES, RECEIPTS], [RECEIPTS]], BUSY);

    await deleteBoth();

    // The page shows its error both above the table and inside the (closed) edit dialog.
    await waitFor(() => expect(screen.getAllByText(`Deleted 1 of 2 test sets. ${BUSY}`).length).toBeGreaterThan(0));
    expect(listedNames().some((t) => t.includes('Invoices'))).toBe(false);
    expect(listedNames().some((t) => t.includes('Receipts'))).toBe(true);
    expect(graphql).toHaveBeenCalledWith(
      expect.objectContaining({ query: 'deleteTestSets', variables: { testSetIds: expect.arrayContaining(['invoices', 'receipts']) } }),
    );
  });

  it('says nothing was deleted when the server still lists every selected set', async () => {
    serve([[INVOICES, RECEIPTS]], BUSY);

    await deleteBoth();

    await waitFor(() => expect(screen.getAllByText(`Failed to delete test sets: ${BUSY}`).length).toBeGreaterThan(0));
    expect(screen.queryByText(/Deleted \d+ of/)).toBeNull();
    expect(listedNames()).toHaveLength(2);
  });

  it('says the outcome is unknown when the list cannot be reloaded', async () => {
    serve([[INVOICES, RECEIPTS], null], BUSY);

    await deleteBoth();

    await waitFor(() =>
      expect(
        screen.getAllByText(`Some test sets may not have been deleted, and the list could not be reloaded to show which: ${BUSY}`).length,
      ).toBeGreaterThan(0),
    );
    expect(screen.queryByText(/Failed to delete test sets/)).toBeNull();
  });

  it('clears an earlier success message', async () => {
    const OTHER = row('other', 'Other', '2026-09-01T00:00:00+00:00');
    // A successful delete updates the list locally, so the next load is the partial one's reload.
    serve([[INVOICES, RECEIPTS, OTHER], [RECEIPTS]], true, BUSY);
    await renderPage();
    // The oldest set sorts last; delete it alone first, which succeeds.
    const rows = listedNames();
    deleteSelected(rows.findIndex((t) => t.includes('Other')) + 1);
    await waitFor(() => expect(screen.getByText('Successfully deleted 1 test set')).toBeTruthy());

    deleteSelected();

    await waitFor(() => expect(screen.getAllByText(`Deleted 1 of 2 test sets. ${BUSY}`).length).toBeGreaterThan(0));
    expect(screen.queryByText('Successfully deleted 1 test set')).toBeNull();
  });
});
