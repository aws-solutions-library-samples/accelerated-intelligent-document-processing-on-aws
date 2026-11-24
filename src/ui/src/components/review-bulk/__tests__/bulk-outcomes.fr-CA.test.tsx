// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The bulk job outcome and status labels under `fr-CA`. With a fixture catalog the outcome
 * counts, the results table's Outcome and Detail cells and its outcome filter, a job's status,
 * and the job type and mode labels read French, while the results' Excel export keeps its
 * English labels. Under `en` the same renders read today's English. Document names stay as
 * stored, and a code the UI does not know shows as it is in either language.
 */
import React from 'react';
import { act, screen } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { I18n } from 'aws-amplify/utils';
import { createIntl } from 'react-intl';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { onIntlError } from '../../../i18n/LocaleProvider';
import { renderWithIntl } from '../../../test-utils/renderWithIntl';

// One mock for every render, so no render hands out a fresh function.
const { exportToExcel } = vi.hoisted(() => ({ exportToExcel: vi.fn() }));

vi.mock('virtual:i18n-catalogs', () => import('../../../test-utils/i18nCatalogsMock'));
vi.mock('../../common/download-func', () => ({ exportToExcel }));

import BulkResultsTable from '../BulkResultsTable';
import { detailLabel, jobModeLabel, jobStatusView, jobTypeLabel, outcomeView } from '../bulk-outcomes';
import { JobStatus } from '../JobsPage';
import OutcomeCounts from '../OutcomeCounts';

beforeEach(() => {
  exportToExcel.mockReset();
});

afterEach(() => {
  window.localStorage.clear();
  document.documentElement.lang = '';
  I18n.setLanguage('');
});

/** The strings the assertions read; the rest fall back to English, as the app's error policy allows. */
const FR = {
  'reviewBulk.outcomes.outcome.skipped': 'Ignoré',
  'reviewBulk.outcomes.outcome.heldByOther': 'Détenu par un autre réviseur',
  'reviewBulk.outcomes.detail.finalizeRetryPending': 'L’exécution de fin sera relancée',
  'reviewBulk.outcomes.jobStatus.running': 'En cours',
  'reviewBulk.outcomes.jobStatus.cancelled': 'Annulé',
  'reviewBulk.outcomes.jobType.skipAllReviews': 'Ignorer toutes les révisions',
  'reviewBulk.outcomes.jobMode.profile': 'Par profil',
  'reviewBulk.outcomeCounts.count': '{label} : {count, number}',
  'reviewBulk.bulkResultsTable.allOutcomes': 'Tous les résultats',
  'reviewBulk.jobsPage.stalled': 'Bloqué',
};
const FR_CA = { locale: 'fr-CA', enabledLocales: ['en', 'fr-CA'], messages: FR } as const;

/** Two skipped documents, one with a detail, and one another reviewer holds. */
const ROWS = [
  { objectKey: 'a.pdf', outcome: 'SKIPPED', kind: 'legacy', detail: null },
  { objectKey: 'b.pdf', outcome: 'HELD_BY_OTHER', kind: 'queue', detail: null },
  { objectKey: 'c.pdf', outcome: 'SKIPPED', kind: 'queue', detail: 'finalize_retry_pending' },
];
/** What the export receives once the filter shows only the skipped documents. */
const SKIPPED_EXPORT = [
  { Document: 'a.pdf', Outcome: 'Skipped', Detail: '' },
  { Document: 'c.pdf', Outcome: 'Skipped', Detail: 'Finishing run will be retried' },
];

/** A dry run's counts: two known outcomes, one the UI does not know, and a zero that is left out. */
const COUNTS = [
  { outcome: 'SKIPPED', count: 3 },
  { outcome: 'HELD_BY_OTHER', count: 1 },
  { outcome: 'SOMETHING_NEW', count: 2 },
  { outcome: 'NOT_ATTEMPTED', count: 0 },
];

const JobStatuses = () => (
  <>
    <JobStatus job={{ status: 'RUNNING', stalled: true }} />
    <JobStatus job={{ status: 'CANCELLED', stalled: false }} />
    <JobStatus job={{ status: 'PAUSED', stalled: false }} />
  </>
);

const table = () => {
  const found = createWrapper(document.body).findTable();
  if (!found) throw new Error('results table not rendered');
  return found;
};
const column = (index: number) => [1, 2, 3].map((row) => table().findBodyCell(row, index)?.getElement().textContent);
const outcomeFilter = () => {
  const found = createWrapper(document.body).findSelect();
  if (!found) throw new Error('outcome filter not rendered');
  return found;
};
const chosenOutcome = () => outcomeFilter().findTrigger().getElement().textContent;
const filterOptions = () => {
  act(() => outcomeFilter().openDropdown());
  return outcomeFilter()
    .findDropdown()
    .findOptions()
    .map((o) => o.findLabel().getElement().textContent);
};
/** Chooses the Skipped outcome, then exports the rows shown. */
const exportSkipped = () => {
  act(() => outcomeFilter().selectOptionByValue('SKIPPED'));
  expect(table().findRows()).toHaveLength(2);
  act(() => table().findHeaderSlot()!.findButton()!.click());
};

// Results table columns: Document, Outcome, Detail.
const OUTCOME = 2;
const DETAIL = 3;

describe('the bulk outcome labels under fr-CA', () => {
  it('word the outcome counts in French, and show an unknown outcome as it is', async () => {
    renderWithIntl(<OutcomeCounts counts={COUNTS} />, FR_CA);
    // Awaited: the catalog arrives asynchronously, as the chunk does in the browser.
    expect(await screen.findByText('Ignoré : 3')).toBeInTheDocument();
    expect(screen.getByText('Détenu par un autre réviseur : 1')).toBeInTheDocument();
    expect(screen.getByText('SOMETHING_NEW : 2')).toBeInTheDocument();
    expect(screen.queryByText(/Skipped|Held by another reviewer/)).toBeNull();
  });
  it('word the results table and its outcome filter in French, while the Excel export stays English', async () => {
    renderWithIntl(<BulkResultsTable results={ROWS} exportName="results" />, FR_CA);
    expect(await screen.findByText('L’exécution de fin sera relancée')).toBeInTheDocument();
    expect(column(OUTCOME)).toEqual(['Ignoré', 'Détenu par un autre réviseur', 'Ignoré']);
    expect(column(DETAIL)).toEqual(['-', '-', 'L’exécution de fin sera relancée']);
    expect(chosenOutcome()).toBe('Tous les résultats');
    expect(filterOptions()).toEqual(['Tous les résultats', 'Ignoré', 'Détenu par un autre réviseur']);
    exportSkipped();
    expect(chosenOutcome()).toBe('Ignoré');
    expect(exportToExcel).toHaveBeenCalledWith(SKIPPED_EXPORT, 'results');
  });
  it('word a job’s status and its Stalled badge in French', async () => {
    renderWithIntl(<JobStatuses />, FR_CA);
    expect(await screen.findByText('En cours')).toBeInTheDocument();
    expect(screen.getByText('Bloqué')).toBeInTheDocument();
    expect(screen.getByText('Annulé')).toBeInTheDocument();
    expect(screen.getByText('PAUSED')).toBeInTheDocument();
    expect(screen.queryByText(/Running|Stalled|Cancelled/)).toBeNull();
  });
  it('word the job type and mode in French, and keep each code’s StatusIndicator type', () => {
    const intl = createIntl({ locale: 'fr-CA', defaultLocale: 'en', messages: FR, onError: onIntlError });
    expect(jobTypeLabel('SKIP_ALL_REVIEWS', intl)).toBe('Ignorer toutes les révisions');
    expect(jobModeLabel('PROFILE', intl)).toBe('Par profil');
    // An id the fixture lacks falls back to its English; an unknown code shows as it is.
    expect(jobModeLabel('SELECTED', intl)).toBe('Selected documents');
    expect(jobTypeLabel('SOMETHING_NEW', intl)).toBe('SOMETHING_NEW');
    expect(outcomeView('HELD_BY_OTHER', intl)).toEqual({ label: 'Détenu par un autre réviseur', type: 'warning' });
    expect(outcomeView('SOMETHING_NEW', intl)).toEqual({ label: 'SOMETHING_NEW', type: 'info' });
    expect(jobStatusView('RUNNING', intl)).toEqual({ label: 'En cours', type: 'in-progress' });
    expect(detailLabel('finalize_retry_pending', intl)).toBe('L’exécution de fin sera relancée');
    expect(detailLabel(null, intl)).toBe('');
  });
});

describe('the bulk outcome labels under en', () => {
  it('word the outcome counts in English, and show an unknown outcome as it is', () => {
    renderWithIntl(<OutcomeCounts counts={COUNTS} />);
    expect(screen.getByText('Skipped: 3')).toBeInTheDocument();
    expect(screen.getByText('Held by another reviewer: 1')).toBeInTheDocument();
    expect(screen.getByText('SOMETHING_NEW: 2')).toBeInTheDocument();
  });
  it('word the results table and its outcome filter in English, and export the same English', () => {
    renderWithIntl(<BulkResultsTable results={ROWS} exportName="results" />);
    expect(column(OUTCOME)).toEqual(['Skipped', 'Held by another reviewer', 'Skipped']);
    expect(column(DETAIL)).toEqual(['-', '-', 'Finishing run will be retried']);
    expect(chosenOutcome()).toBe('All outcomes');
    expect(filterOptions()).toEqual(['All outcomes', 'Skipped', 'Held by another reviewer']);
    exportSkipped();
    expect(chosenOutcome()).toBe('Skipped');
    expect(exportToExcel).toHaveBeenCalledWith(SKIPPED_EXPORT, 'results');
  });
  it('word a job’s status and its Stalled badge in English', () => {
    renderWithIntl(<JobStatuses />);
    expect(screen.getByText('Running')).toBeInTheDocument();
    expect(screen.getByText('Stalled')).toBeInTheDocument();
    expect(screen.getByText('Cancelled')).toBeInTheDocument();
    expect(screen.getByText('PAUSED')).toBeInTheDocument();
  });
  it('word the job type and mode in English, with or without intl, and keep each code’s StatusIndicator type', () => {
    const intl = createIntl({ locale: 'en', defaultLocale: 'en', onError: onIntlError });
    for (const en of [intl, undefined]) {
      expect(jobTypeLabel('SKIP_ALL_REVIEWS', en)).toBe('Skip All Reviews');
      expect(jobModeLabel('PROFILE', en)).toBe('By profile');
      expect(jobModeLabel('SELECTED', en)).toBe('Selected documents');
      expect(jobTypeLabel('SOMETHING_NEW', en)).toBe('SOMETHING_NEW');
      expect(outcomeView('HELD_BY_OTHER', en)).toEqual({ label: 'Held by another reviewer', type: 'warning' });
      expect(outcomeView('SOMETHING_NEW', en)).toEqual({ label: 'SOMETHING_NEW', type: 'info' });
      expect(jobStatusView('RUNNING', en)).toEqual({ label: 'Running', type: 'in-progress' });
      expect(detailLabel('finalize_retry_pending', en)).toBe('Finishing run will be retried');
      expect(detailLabel(null, en)).toBe('');
    }
  });
});
