// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Home › Jobs: bulk Skip All Reviews runs, newest first, in the same table format as
// the Inbox (find, pagination, settings). A Reviewer sees their own; an Admin sees
// every job. Each row's menu cancels a queued or running job, or deletes a finished one.
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useCollection } from '@cloudscape-design/collection-hooks';
import type { ButtonDropdownProps, TableProps } from '@cloudscape-design/components';
import {
  Alert,
  Button,
  ButtonDropdown,
  CollectionPreferences,
  Link,
  Pagination,
  SpaceBetween,
  StatusIndicator,
  Table,
  TextFilter,
} from '@cloudscape-design/components';

import useAppContext from '../../contexts/app';
import usePolling from '../../hooks/use-polling';
import useUserRole from '../../hooks/use-user-role';
import { describeApiError } from '../../hooks/utils/graphql-error';
import { jobDetailHref } from '../../routes/constants';
import { formatDateTime } from '../../utils/date-time';
import type { BulkReviewJob } from '../../graphql/generated/schema-types';
import { paginationLabels } from '../common/labels';
import useLocalStorage from '../common/local-storage';
import { TableEmptyState, TableHeader, TableNoMatchState, getFilterCounterText } from '../common/table';
import { cancelBulkJob, deleteBulkJob, listBulkJobs } from './bulk-review-api';
import { ACTIVE_JOB_STATUSES, JOB_MODE_LABELS, JOB_TYPE_LABELS, jobStatusView } from './bulk-outcomes';
import JobActionModal from './JobActionModals';
import type { JobAction } from './JobActionModals';

/** Jobs are kept for at most 30 days, so a few pages hold all of them. */
const PAGE_LIMIT = 100;
const MAX_PAGES = 10;

export const skippedCount = (job: Pick<BulkReviewJob, 'counts'>): number => job.counts.find((c) => c.outcome === 'SKIPPED')?.count ?? 0;

export const JobStatus = ({ job }: { job: Pick<BulkReviewJob, 'status' | 'stalled'> }): React.JSX.Element => {
  const view = jobStatusView(job.status);
  return (
    <SpaceBetween direction="horizontal" size="xxs">
      <StatusIndicator type={view.type}>{view.label}</StatusIndicator>
      {job.stalled && <StatusIndicator type="warning">Stalled</StatusIndicator>}
    </SpaceBetween>
  );
};

export const JOB_COLUMN_OPTIONS = [
  { id: 'jobId', label: 'Job ID', alwaysVisible: true },
  { id: 'type', label: 'Type' },
  { id: 'status', label: 'Status' },
  { id: 'mode', label: 'Mode' },
  { id: 'profile', label: 'Profile' },
  { id: 'requestedBy', label: 'Requested by' },
  { id: 'created', label: 'Created' },
  { id: 'finished', label: 'Finished' },
  { id: 'processed', label: 'Processed / Total' },
  { id: 'skipped', label: 'Skipped' },
  { id: 'actions', label: 'Action', alwaysVisible: true },
];

interface JobsTablePreferences {
  pageSize: number;
  contentDisplay: { id: string; visible: boolean }[];
}

/** The columns shown until the user changes them in the table's settings. */
export const DEFAULT_VISIBLE_JOB_COLUMNS = ['jobId', 'type', 'requestedBy', 'created', 'finished', 'actions'];

export const DEFAULT_JOBS_PREFERENCES: JobsTablePreferences = {
  pageSize: 20,
  contentDisplay: JOB_COLUMN_OPTIONS.map((c) => ({ id: c.id, visible: DEFAULT_VISIBLE_JOB_COLUMNS.includes(c.id) })),
};

/** The row menu: Cancel for a queued or running job, Delete once it has finished. */
export const jobMenuItems = (job: Pick<BulkReviewJob, 'status'>): ButtonDropdownProps.Item[] => {
  const active = ACTIVE_JOB_STATUSES.has(job.status);
  return [
    ...(active ? [{ id: 'cancel', text: 'Cancel' }] : []),
    {
      id: 'delete',
      text: 'Delete',
      disabled: active,
      disabledReason: active ? 'Cancel the job, or wait for it to finish, before deleting it' : undefined,
    },
  ];
};

const searchText = (j: BulkReviewJob): string =>
  [j.jobId, JOB_TYPE_LABELS[j.jobType] ?? j.jobType, jobStatusView(j.status).label, j.requestedBy, j.profile ?? '', j.reason]
    .join(' ')
    .toLowerCase();

const JobsPage = (): React.JSX.Element => {
  // 1. Hooks
  const { setErrorMessage, setSuccessMessage } = useAppContext();
  const { isAdmin } = useUserRole();
  const [jobs, setJobs] = useState<BulkReviewJob[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [target, setTarget] = useState<{ action: JobAction; job: BulkReviewJob } | null>(null);
  const [busy, setBusy] = useState(false);
  const [preferences, setPreferences] = useLocalStorage<JobsTablePreferences>('jobs-table-preferences', DEFAULT_JOBS_PREFERENCES);
  const headingRef = useRef<HTMLSpanElement>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const all: BulkReviewJob[] = [];
      let nextToken: string | null | undefined = null;
      for (let page = 0; page < MAX_PAGES; page += 1) {
        const result = await listBulkJobs({ limit: PAGE_LIMIT, nextToken });
        all.push(...result.jobs);
        nextToken = result.nextToken;
        if (!nextToken) break;
      }
      setJobs(all);
      setError(null);
    } catch (e) {
      setError(describeApiError(e, 'load jobs'));
    } finally {
      setLoading(false);
    }
  }, []);

  const { items, collectionProps, filterProps, filteredItemsCount, paginationProps, actions } = useCollection(jobs ?? [], {
    filtering: {
      empty: (
        <TableEmptyState
          resourceName="Job"
          title="No jobs yet"
          description="Bulk Skip All Reviews runs that go to the background appear here."
        />
      ),
      noMatch: <TableNoMatchState onClearFilter={() => actions.setFiltering('')} />,
      filteringFunction: (j, text) => searchText(j).includes(text.trim().toLowerCase()),
    },
    pagination: { pageSize: preferences.pageSize },
    sorting: { defaultState: { sortingColumn: { sortingField: 'createdAt' }, isDescending: true } },
  });

  // 2. Effects
  useEffect(() => {
    void load();
  }, [load]);
  usePolling(() => void load(), { enabled: !!jobs?.some((j) => ACTIVE_JOB_STATUSES.has(j.status)), intervalMs: 10_000 });

  // 4. Handlers
  const closeDialog = () => {
    setTarget(null);
    headingRef.current?.focus();
  };

  const onConfirm = async () => {
    if (!target) return;
    const { action, job } = target;
    setBusy(true);
    try {
      if (action === 'cancel') {
        const cancelled = await cancelBulkJob(job.jobId);
        setJobs((prev) => (prev ?? []).map((j) => (j.jobId === job.jobId ? cancelled : j)));
        setSuccessMessage(`Job ${job.jobId} cancelled.`);
      } else {
        await deleteBulkJob(job.jobId);
        setJobs((prev) => (prev ?? []).filter((j) => j.jobId !== job.jobId));
        setSuccessMessage(`Job ${job.jobId} deleted.`);
      }
    } catch (e) {
      setErrorMessage(describeApiError(e, action === 'cancel' ? 'cancel the job' : 'delete the job'));
      void load();
    } finally {
      setBusy(false);
      closeDialog();
    }
  };

  // 5. Render
  const columnDefinitions: TableProps.ColumnDefinition<BulkReviewJob>[] = [
    { id: 'jobId', header: 'Job ID', cell: (j) => <Link href={jobDetailHref(j.jobId)}>{j.jobId}</Link>, sortingField: 'jobId' },
    { id: 'type', header: 'Type', cell: (j) => JOB_TYPE_LABELS[j.jobType] ?? j.jobType, sortingField: 'jobType' },
    { id: 'status', header: 'Status', cell: (j) => <JobStatus job={j} />, sortingField: 'status' },
    { id: 'mode', header: 'Mode', cell: (j) => JOB_MODE_LABELS[j.mode] ?? j.mode, sortingField: 'mode' },
    { id: 'profile', header: 'Profile', cell: (j) => j.profile ?? '-', sortingField: 'profile' },
    { id: 'requestedBy', header: 'Requested by', cell: (j) => j.requestedBy, sortingField: 'requestedBy' },
    { id: 'created', header: 'Created', cell: (j) => formatDateTime(j.createdAt), sortingField: 'createdAt' },
    { id: 'finished', header: 'Finished', cell: (j) => (j.finishedAt ? formatDateTime(j.finishedAt) : '-'), sortingField: 'finishedAt' },
    {
      id: 'processed',
      header: 'Processed / Total',
      cell: (j) => `${j.processed.toLocaleString()} / ${j.total.toLocaleString()}`,
    },
    { id: 'skipped', header: 'Skipped', cell: (j) => skippedCount(j).toLocaleString() },
    {
      id: 'actions',
      header: 'Action',
      cell: (j) => (
        <ButtonDropdown
          variant="inline-icon"
          ariaLabel={`Actions for ${j.jobId}`}
          expandToViewport
          disabled={busy}
          items={jobMenuItems(j)}
          onItemClick={({ detail }) => setTarget({ action: detail.id as JobAction, job: j })}
        />
      ),
    },
  ];

  return (
    <SpaceBetween size="s">
      {error && (
        <Alert
          type="error"
          header="Could not load jobs"
          action={
            <Button onClick={() => void load()} loading={loading}>
              Retry
            </Button>
          }
        >
          {error}
        </Alert>
      )}
      <Table
        {...collectionProps}
        trackBy="jobId"
        columnDefinitions={columnDefinitions}
        columnDisplay={preferences.contentDisplay}
        items={items}
        loading={loading && jobs === null}
        loadingText="Loading jobs"
        header={
          <TableHeader
            title={
              <span ref={headingRef} tabIndex={-1}>
                Jobs
              </span>
            }
            totalItems={jobs ?? []}
            description={isAdmin ? 'Bulk Skip All Reviews runs by every user.' : 'Your bulk Skip All Reviews runs.'}
            actionButtons={<Button iconName="refresh" ariaLabel="Refresh jobs" onClick={() => void load()} loading={loading} />}
          />
        }
        filter={
          <TextFilter
            {...filterProps}
            filteringPlaceholder="Find jobs"
            filteringAriaLabel="Find jobs"
            countText={getFilterCounterText(filteredItemsCount ?? 0)}
          />
        }
        pagination={<Pagination {...paginationProps} ariaLabels={paginationLabels} />}
        preferences={
          <CollectionPreferences
            title="Preferences"
            confirmLabel="Confirm"
            cancelLabel="Cancel"
            preferences={preferences}
            onConfirm={({ detail }) =>
              setPreferences({
                pageSize: detail.pageSize ?? DEFAULT_JOBS_PREFERENCES.pageSize,
                contentDisplay: [...(detail.contentDisplay ?? DEFAULT_JOBS_PREFERENCES.contentDisplay)],
              })
            }
            pageSizePreference={{
              title: 'Page size',
              options: [
                { value: 10, label: '10 jobs' },
                { value: 20, label: '20 jobs' },
                { value: 50, label: '50 jobs' },
              ],
            }}
            contentDisplayPreference={{ title: 'Column preferences', options: JOB_COLUMN_OPTIONS }}
          />
        }
      />
      <JobActionModal
        action={target?.action ?? null}
        jobId={target?.job.jobId ?? ''}
        isLoading={busy}
        onDismiss={closeDialog}
        onConfirm={() => void onConfirm()}
      />
    </SpaceBetween>
  );
};

export default JobsPage;
