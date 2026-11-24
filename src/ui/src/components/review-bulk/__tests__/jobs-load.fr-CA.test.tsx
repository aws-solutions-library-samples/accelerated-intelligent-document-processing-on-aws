// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Home › Jobs under `fr-CA`. With a fixture catalog the job list is requested once, not again
 * when the catalog arrives, and a failed load reads as one French sentence, its action phrase
 * included: the page holds what the load threw and words it at render. Under `en` the same
 * renders read today's English. The server's error text stays as it is.
 */
import React from 'react';
import { screen } from '@testing-library/react';
import { I18n } from 'aws-amplify/utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { renderWithIntl } from '../../../test-utils/renderWithIntl';

// One value per mocked hook for every render, so no render hands out a fresh object.
const state = vi.hoisted(() => ({
  api: { listBulkJobs: vi.fn(), getBulkJob: vi.fn(), cancelBulkJob: vi.fn(), deleteBulkJob: vi.fn() },
  role: { isAdmin: false },
  app: { setErrorMessage: vi.fn(), setSuccessMessage: vi.fn() },
}));

vi.mock('virtual:i18n-catalogs', () => import('../../../test-utils/i18nCatalogsMock'));
vi.mock('../bulk-review-api', () => state.api);
vi.mock('../../../hooks/use-user-role', () => ({ default: () => state.role }));
vi.mock('../../../contexts/app', () => ({ default: () => state.app }));
// The job below has finished, so the page would not poll; mocked so no interval outlives a test.
vi.mock('../../../hooks/use-polling', () => ({ default: () => undefined }));

import JobsPage from '../JobsPage';

/** The strings the assertions read; the rest fall back to English, as the app's error policy allows. */
const FR = {
  'reviewBulk.jobsPage.title': 'Travaux',
  'reviewBulk.jobsPage.loadFailed': 'Impossible de charger les travaux',
  'reviewBulk.jobsPage.loadJobsAction': 'charger les travaux',
  'common.apiError.failed': 'Échec de l’opération « {action} » : {error}',
};
const FR_CA = { locale: 'fr-CA', enabledLocales: ['en', 'fr-CA'], messages: FR } as const;

/** A fixed instant, stored as the jobs API returns it: the UTC ISO string. */
const AT = new Date(1_700_000_000_000).toISOString();

/** A finished Skip All Reviews job, with the fields the Jobs table reads. */
const JOB = {
  jobId: 'brj-1',
  jobType: 'SKIP_ALL_REVIEWS',
  status: 'SUCCEEDED',
  mode: 'PROFILE',
  profile: 'claims',
  reason: 'Duplicate document',
  requestedBy: 'reviewer',
  total: 10,
  processed: 10,
  counts: [{ outcome: 'SKIPPED', count: 10 }],
  stalled: false,
  createdAt: AT,
  finishedAt: AT,
};

/** What the dispatcher sends for a resolver that raised, as the client throws it. */
const SERVER_ERROR = { errors: [{ message: 'boom', errorType: 'InternalError' }] };

beforeEach(() => {
  state.api.listBulkJobs.mockReset();
  state.app.setErrorMessage.mockReset();
});

afterEach(() => {
  window.localStorage.clear();
  document.documentElement.lang = '';
  I18n.setLanguage('');
});

describe('Home › Jobs under fr-CA', () => {
  // The catalog arrives after the first render, as a new `intl`; that must not load the jobs again.
  it('requests its job list once, not again when the catalog arrives', async () => {
    state.api.listBulkJobs.mockResolvedValue({ jobs: [JOB], nextToken: null });
    renderWithIntl(<JobsPage />, FR_CA);
    // Awaited: the catalog arrives asynchronously, as the chunk does in the browser.
    expect(await screen.findByText('Travaux')).toBeInTheDocument();
    expect(await screen.findByText('brj-1')).toBeInTheDocument();
    expect(state.api.listBulkJobs).toHaveBeenCalledTimes(1);
  });

  it('reads a failed load as one French sentence', async () => {
    state.api.listBulkJobs.mockRejectedValue(SERVER_ERROR);
    renderWithIntl(<JobsPage />, FR_CA);
    expect(await screen.findByText('Échec de l’opération « charger les travaux » : boom')).toBeInTheDocument();
    expect(screen.getByText('Impossible de charger les travaux')).toBeInTheDocument();
    expect(screen.queryByText(/Failed to/)).toBeNull();
    expect(state.api.listBulkJobs).toHaveBeenCalledTimes(1);
  });
});

describe('Home › Jobs under en', () => {
  it('requests its job list once', async () => {
    state.api.listBulkJobs.mockResolvedValue({ jobs: [JOB], nextToken: null });
    renderWithIntl(<JobsPage />);
    expect(await screen.findByText('brj-1')).toBeInTheDocument();
    expect(screen.getByText('Jobs')).toBeInTheDocument();
    expect(state.api.listBulkJobs).toHaveBeenCalledTimes(1);
  });

  it('reads a failed load in English', async () => {
    state.api.listBulkJobs.mockRejectedValue(SERVER_ERROR);
    renderWithIntl(<JobsPage />);
    expect(await screen.findByText('Failed to load jobs: boom')).toBeInTheDocument();
    expect(screen.getByText('Could not load jobs')).toBeInTheDocument();
    expect(state.api.listBulkJobs).toHaveBeenCalledTimes(1);
  });
});
