// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/** Home › Jobs and a job's page: columns, polling while active, results, not found. */

import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const { api, role, polling } = vi.hoisted(() => ({
  api: { listBulkJobs: vi.fn(), getBulkJob: vi.fn() },
  role: { current: { isAdmin: false } },
  polling: { calls: [] as { enabled: boolean; intervalMs: number }[] },
}));
vi.mock('../bulk-review-api', () => api);
vi.mock('../../../hooks/use-user-role', () => ({ default: () => role.current }));
vi.mock('../../../contexts/app', () => ({ default: () => ({ setErrorMessage: vi.fn() }) }));
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
  polling.calls = [];
  role.current = { isAdmin: false };
});

describe('JobsPage', () => {
  it('lists jobs with their status and counts, and polls while one is active', async () => {
    api.listBulkJobs.mockResolvedValue({ jobs: [job()], nextToken: null });
    render(
      <MemoryRouter>
        <JobsPage />
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByText('brj-1')).toBeInTheDocument());
    const table = createWrapper().findTable()!;
    const headers = table.findColumnHeaders().map((h) => h.getElement().textContent);
    expect(headers).toEqual(['Job ID', 'Type', 'Status', 'Mode', 'Profile', 'Created', 'Finished', 'Processed / Total', 'Skipped']);
    expect(table.getElement().textContent).toContain('Skip All Reviews');
    expect(table.getElement().textContent).toContain('Running');
    expect(table.getElement().textContent).toContain('4 / 10');
    expect(polling.calls.at(-1)).toEqual({ enabled: true, intervalMs: 10_000 });
  });

  it('shows Requested by to Admins, and stops polling when every job is final', async () => {
    role.current = { isAdmin: true };
    api.listBulkJobs.mockResolvedValue({ jobs: [job({ status: 'SUCCEEDED' })], nextToken: null });
    render(
      <MemoryRouter>
        <JobsPage />
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByText('brj-1')).toBeInTheDocument());
    const headers = createWrapper()
      .findTable()!
      .findColumnHeaders()
      .map((h) => h.getElement().textContent);
    expect(headers).toContain('Requested by');
    expect(polling.calls.at(-1)?.enabled).toBe(false);
  });

  it('explains an empty list', async () => {
    api.listBulkJobs.mockResolvedValue({ jobs: [], nextToken: null });
    render(
      <MemoryRouter>
        <JobsPage />
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByText('No jobs yet')).toBeInTheDocument());
  });
});

const renderDetail = () =>
  render(
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

  it('reads another user’s job as not found', async () => {
    api.getBulkJob.mockRejectedValue({ errors: [{ message: 'NOT_FOUND: no such job' }] });
    renderDetail();
    await waitFor(() => expect(screen.getByText('Job not found, or not yours.')).toBeInTheDocument());
  });
});
