// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Home › Jobs and a job's page: the default columns and the Inbox table format (find,
 * pagination, settings), polling while active, Cancel and Delete in the row menu,
 * results, not found.
 */

import React from 'react';
import { act, screen, waitFor } from '@testing-library/react';
import { renderWithIntl } from '../../../test-utils/renderWithIntl';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const { api, role, polling } = vi.hoisted(() => ({
  api: { listBulkJobs: vi.fn(), getBulkJob: vi.fn(), cancelBulkJob: vi.fn(), deleteBulkJob: vi.fn() },
  role: { current: { isAdmin: false } },
  polling: { calls: [] as { enabled: boolean; intervalMs: number }[] },
}));
vi.mock('../bulk-review-api', () => api);
vi.mock('../../../hooks/use-user-role', () => ({ default: () => role.current }));
vi.mock('../../../contexts/app', () => ({ default: () => ({ setErrorMessage: vi.fn(), setSuccessMessage: vi.fn() }) }));
vi.mock('../../../hooks/use-polling', () => ({
  default: (_cb: unknown, options: { enabled: boolean; intervalMs: number }) => {
    polling.calls.push(options);
  },
}));

import JobsPage from '../JobsPage';
import JobDetailPage from '../JobDetailPage';

const job = (over = {}) => ({
  jobId: 'brj-1',
  jobType: 'SKIP_ALL_REVIEWS',
  kind: 'ASYNC',
  status: 'RUNNING',
  mode: 'PROFILE',
  profile: 'claims',
  submittedFrom: '2026-01-01T00:00:00.000Z',
  submittedTo: '2026-01-08T00:00:00.000Z',
  reason: 'Duplicate document',
  note: null,
  resubmit: true,
  requestedBy: 'rev@example.com',
  total: 10,
  processed: 4,
  counts: [
    { outcome: 'SKIPPED', count: 3 },
    { outcome: 'HELD_BY_OTHER', count: 1 },
  ],
  limitReached: false,
  stalled: false,
  error: null,
  createdAt: '2026-01-09T00:00:00.000Z',
  startedAt: null,
  finishedAt: null,
  ...over,
});

beforeEach(() => {
  api.listBulkJobs.mockReset();
  api.getBulkJob.mockReset();
  api.cancelBulkJob.mockReset();
  api.deleteBulkJob.mockReset();
  localStorage.clear();
  polling.calls = [];
  role.current = { isAdmin: false };
});

const renderJobs = async () => {
  renderWithIntl(
    <MemoryRouter>
      <JobsPage />
    </MemoryRouter>,
  );
  await waitFor(() => expect(screen.getByText('brj-1')).toBeInTheDocument());
  return createWrapper().findTable()!;
};
const headersOf = (table: ReturnType<typeof createWrapper>['findTable'] extends () => infer T ? NonNullable<T> : never) =>
  table.findColumnHeaders().map((h) => h.getElement().textContent);

describe('JobsPage', () => {
  it('shows Job ID, Type, Requested by, Created, Finished and Action first, and polls while a job is active', async () => {
    api.listBulkJobs.mockResolvedValue({ jobs: [job()], nextToken: null });
    const table = await renderJobs();
    expect(headersOf(table)).toEqual(['Job ID', 'Type', 'Requested by', 'Created', 'Finished', 'Action']);
    expect(table.getElement().textContent).toContain('Skip All Reviews');
    expect(table.getElement().textContent).toContain('rev@example.com');
    expect(polling.calls.at(-1)).toEqual({ enabled: true, intervalMs: 10_000 });
  });

  it('has the Inbox table format: find, pagination and settings with column choices', async () => {
    api.listBulkJobs.mockResolvedValue({ jobs: [job(), job({ jobId: 'brj-2', requestedBy: 'other@example.com' })], nextToken: null });
    const table = await renderJobs();
    expect(table.findTextFilter()).not.toBeNull();
    expect(table.findPagination()).not.toBeNull();
    table.findTextFilter()!.findInput().setInputValue('other@');
    await waitFor(() => expect(table.findRows()).toHaveLength(1));
    const preferences = table.findCollectionPreferences()!;
    preferences.findTriggerButton().click();
    const options = preferences
      .findModal()!
      .findContentDisplayPreference()!
      .findOptions()
      .map((o) => o.findLabel().getElement().textContent);
    expect(options).toEqual(expect.arrayContaining(['Status', 'Mode', 'Profile', 'Processed / Total', 'Skipped']));
  });

  it('reads every page of jobs', async () => {
    api.listBulkJobs
      .mockResolvedValueOnce({ jobs: [job()], nextToken: 't1' })
      .mockResolvedValueOnce({ jobs: [job({ jobId: 'brj-2' })], nextToken: null });
    const table = await renderJobs();
    await waitFor(() => expect(table.findRows()).toHaveLength(2));
    expect(api.listBulkJobs).toHaveBeenLastCalledWith({ limit: 100, nextToken: 't1' });
  });

  it('cancels a running job from its row menu after confirming', async () => {
    api.listBulkJobs.mockResolvedValue({ jobs: [job()], nextToken: null });
    api.cancelBulkJob.mockResolvedValue(job({ status: 'CANCELLED', finishedAt: '2026-01-09T01:00:00.000Z' }));
    const table = await renderJobs();
    const menu = table.findBodyCell(1, 6)!.findButtonDropdown()!;
    menu.openDropdown();
    expect(menu.findItemById('delete')!.getElement().querySelector('[aria-disabled="true"]')).not.toBeNull();
    act(() => menu.findItemById('cancel')!.click());
    const modal = createWrapper().findModal()!;
    expect(modal.findHeader().getElement().textContent).toBe('Cancel job');
    act(() => modal.findFooter()!.findAllButtons().at(-1)!.click());
    await waitFor(() => expect(api.cancelBulkJob).toHaveBeenCalledWith('brj-1'));
    await waitFor(() => expect(polling.calls.at(-1)?.enabled).toBe(false));
  });

  it('deletes a finished job, and offers no Cancel for it', async () => {
    api.listBulkJobs.mockResolvedValue({ jobs: [job({ status: 'SUCCEEDED' })], nextToken: null });
    api.deleteBulkJob.mockResolvedValue(undefined);
    const table = await renderJobs();
    const menu = table.findBodyCell(1, 6)!.findButtonDropdown()!;
    menu.openDropdown();
    expect(menu.findItemById('cancel')).toBeNull();
    act(() => menu.findItemById('delete')!.click());
    const modal = createWrapper().findModal()!;
    expect(modal.findHeader().getElement().textContent).toBe('Delete job');
    act(() => modal.findFooter()!.findAllButtons().at(-1)!.click());
    await waitFor(() => expect(api.deleteBulkJob).toHaveBeenCalledWith('brj-1'));
    await waitFor(() => expect(screen.queryByText('brj-1')).toBeNull());
  });

  it('explains an empty list', async () => {
    api.listBulkJobs.mockResolvedValue({ jobs: [], nextToken: null });
    renderWithIntl(
      <MemoryRouter>
        <JobsPage />
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByText('No jobs yet')).toBeInTheDocument());
  });
});

const renderDetail = () =>
  renderWithIntl(
    <MemoryRouter initialEntries={['/my-work/jobs/brj-1']}>
      <Routes>
        <Route path="/my-work/jobs/:jobId" element={<JobDetailPage />} />
      </Routes>
    </MemoryRouter>,
  );

describe('JobDetailPage', () => {
  it('shows progress, counts and results, and polls while running', async () => {
    api.getBulkJob.mockResolvedValue({
      job: job(),
      results: [{ objectKey: 'a.pdf', outcome: 'SKIPPED', kind: 'legacy', detail: null }],
      nextToken: null,
    });
    renderDetail();
    await waitFor(() => expect(screen.getByText('4 of 10 documents processed')).toBeInTheDocument());
    expect(api.getBulkJob).toHaveBeenCalledWith({ jobId: 'brj-1', limit: 100 });
    expect(screen.getByText('a.pdf')).toBeInTheDocument();
    expect(screen.getByText('Held by another reviewer: 1')).toBeInTheDocument();
    expect(polling.calls.at(-1)).toEqual({ enabled: true, intervalMs: 5_000 });
  });

  it('shows a cancelled job as stopped, and stops polling', async () => {
    api.getBulkJob.mockResolvedValue({
      job: job({ status: 'CANCELLED', finishedAt: '2026-01-09T01:00:00.000Z' }),
      results: [],
      nextToken: null,
    });
    renderDetail();
    await waitFor(() => expect(createWrapper().findProgressBar()!.findResultText()!.getElement().textContent).toBe('Cancelled'));
    expect(polling.calls.at(-1)?.enabled).toBe(false);
  });

  it('reads another user’s job as not found', async () => {
    api.getBulkJob.mockRejectedValue({ errors: [{ message: 'NOT_FOUND: no such job' }] });
    renderDetail();
    await waitFor(() => expect(screen.getByText('Job not found, or not yours.')).toBeInTheDocument());
  });
});
