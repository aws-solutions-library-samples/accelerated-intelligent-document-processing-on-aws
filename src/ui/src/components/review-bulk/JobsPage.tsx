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
import { defineMessages, FormattedMessage, useIntl, type IntlShape, type NoMessageValues } from 'react-intl';

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

const messages = defineMessages<{
  stalled: NoMessageValues;
  cancel: NoMessageValues;
  delete: NoMessageValues;
  deleteDisabled: NoMessageValues;
  loadJobsAction: NoMessageValues;
  cancelJobAction: NoMessageValues;
  deleteJobAction: NoMessageValues;
  resourceName: NoMessageValues;
  emptyTitle: NoMessageValues;
  emptyDescription: NoMessageValues;
  cancelled: { readonly jobId: string };
  deleted: { readonly jobId: string };
  columnJobId: NoMessageValues;
  columnType: NoMessageValues;
  columnStatus: NoMessageValues;
  columnMode: NoMessageValues;
  columnProfile: NoMessageValues;
  columnRequestedBy: NoMessageValues;
  columnCreated: NoMessageValues;
  columnFinished: NoMessageValues;
  columnProcessed: NoMessageValues;
  columnSkipped: NoMessageValues;
  columnAction: NoMessageValues;
  processedOfTotal: { readonly processed: number; readonly total: number };
  actionsFor: { readonly jobId: string };
  loadFailed: NoMessageValues;
  retry: NoMessageValues;
  loading: NoMessageValues;
  title: NoMessageValues;
  descriptionAdmin: NoMessageValues;
  descriptionOwn: NoMessageValues;
  refresh: NoMessageValues;
  find: NoMessageValues;
  preferences: NoMessageValues;
  confirm: NoMessageValues;
  cancelPreferences: NoMessageValues;
  pageSize: NoMessageValues;
  pageSizeOption: { readonly count: number };
  columnPreferences: NoMessageValues;
}>({
  stalled: {
    id: 'reviewBulk.jobsPage.stalled',
    defaultMessage: 'Stalled',
    description: 'Status badge next to a job status when the job has not reported progress for 30 minutes.',
  },
  cancel: {
    id: 'reviewBulk.jobsPage.cancel',
    defaultMessage: 'Cancel',
    description: 'Row menu item of the Jobs table that cancels a queued or running job.',
  },
  delete: {
    id: 'reviewBulk.jobsPage.delete',
    defaultMessage: 'Delete',
    description: 'Row menu item of the Jobs table that deletes a finished job.',
  },
  deleteDisabled: {
    id: 'reviewBulk.jobsPage.deleteDisabled',
    defaultMessage: 'Cancel the job, or wait for it to finish, before deleting it',
    description: 'Why the Delete menu item of a queued or running job is disabled.',
  },
  loadJobsAction: {
    id: 'reviewBulk.jobsPage.loadJobsAction',
    defaultMessage: 'load jobs',
    description:
      'Action phrase completing an API error sentence ("Failed to …: <error>" / "You do not have permission to …") when the job list cannot be loaded.',
  },
  cancelJobAction: {
    id: 'reviewBulk.jobsPage.cancelJobAction',
    defaultMessage: 'cancel the job',
    description:
      'Action phrase completing an API error sentence ("Failed to …: <error>" / "You do not have permission to …") when a job cannot be cancelled.',
  },
  deleteJobAction: {
    id: 'reviewBulk.jobsPage.deleteJobAction',
    defaultMessage: 'delete the job',
    description:
      'Action phrase completing an API error sentence ("Failed to …: <error>" / "You do not have permission to …") when a job cannot be deleted.',
  },
  resourceName: {
    id: 'reviewBulk.jobsPage.resourceName',
    defaultMessage: 'Job',
    description: 'Name of one item of the Jobs table, used by its empty state.',
  },
  emptyTitle: {
    id: 'reviewBulk.jobsPage.emptyTitle',
    defaultMessage: 'No jobs yet',
    description: 'Title of the empty state of the Jobs table.',
  },
  emptyDescription: {
    id: 'reviewBulk.jobsPage.emptyDescription',
    defaultMessage: 'Bulk Skip All Reviews runs that go to the background appear here.',
    description: 'Description of the empty state of the Jobs table.',
  },
  cancelled: {
    id: 'reviewBulk.jobsPage.cancelled',
    defaultMessage: 'Job {jobId} cancelled.',
    description: 'Success notification after a job is cancelled. {jobId} is the job ID.',
  },
  deleted: {
    id: 'reviewBulk.jobsPage.deleted',
    defaultMessage: 'Job {jobId} deleted.',
    description: 'Success notification after a job is deleted. {jobId} is the job ID.',
  },
  columnJobId: {
    id: 'reviewBulk.jobsPage.columnJobId',
    defaultMessage: 'Job ID',
    description: 'Column header (and column preference) of the Jobs table: the job ID.',
  },
  columnType: {
    id: 'reviewBulk.jobsPage.columnType',
    defaultMessage: 'Type',
    description: 'Column header (and column preference) of the Jobs table: the kind of job (e.g. Skip All Reviews).',
  },
  columnStatus: {
    id: 'reviewBulk.jobsPage.columnStatus',
    defaultMessage: 'Status',
    description: 'Column header (and column preference) of the Jobs table: the job status.',
  },
  columnMode: {
    id: 'reviewBulk.jobsPage.columnMode',
    defaultMessage: 'Mode',
    description: 'Column header (and column preference) of the Jobs table: how the job chose documents (selected documents or by profile).',
  },
  columnProfile: {
    id: 'reviewBulk.jobsPage.columnProfile',
    defaultMessage: 'Profile',
    description: 'Column header (and column preference) of the Jobs table: the configuration profile.',
  },
  columnRequestedBy: {
    id: 'reviewBulk.jobsPage.columnRequestedBy',
    defaultMessage: 'Requested by',
    description: 'Column header (and column preference) of the Jobs table: the user who started the job.',
  },
  columnCreated: {
    id: 'reviewBulk.jobsPage.columnCreated',
    defaultMessage: 'Created',
    description: 'Column header (and column preference) of the Jobs table: when the job was created.',
  },
  columnFinished: {
    id: 'reviewBulk.jobsPage.columnFinished',
    defaultMessage: 'Finished',
    description: 'Column header (and column preference) of the Jobs table: when the job finished.',
  },
  columnProcessed: {
    id: 'reviewBulk.jobsPage.columnProcessed',
    defaultMessage: 'Processed / Total',
    description: 'Column header (and column preference) of the Jobs table: documents processed out of the total.',
  },
  columnSkipped: {
    id: 'reviewBulk.jobsPage.columnSkipped',
    defaultMessage: 'Skipped',
    description: 'Column header (and column preference) of the Jobs table: number of reviews skipped.',
  },
  columnAction: {
    id: 'reviewBulk.jobsPage.columnAction',
    defaultMessage: 'Action',
    description: 'Column header (and column preference) of the Jobs table: the row action menu.',
  },
  processedOfTotal: {
    id: 'reviewBulk.jobsPage.processedOfTotal',
    defaultMessage: '{processed, number} / {total, number}',
    description:
      'Cell of the Processed / Total column. {processed} is the number of documents done, {total} the number the job applies to.',
  },
  actionsFor: {
    id: 'reviewBulk.jobsPage.actionsFor',
    defaultMessage: 'Actions for {jobId}',
    description: 'Screen-reader label of the row action menu of the Jobs table. {jobId} is the job ID.',
  },
  loadFailed: {
    id: 'reviewBulk.jobsPage.loadFailed',
    defaultMessage: 'Could not load jobs',
    description: 'Header of the error alert when the job list cannot be loaded.',
  },
  retry: {
    id: 'reviewBulk.jobsPage.retry',
    defaultMessage: 'Retry',
    description: 'Button in the error alert that loads the job list again.',
  },
  loading: {
    id: 'reviewBulk.jobsPage.loading',
    defaultMessage: 'Loading jobs',
    description: 'Loading text of the Jobs table.',
  },
  title: {
    id: 'reviewBulk.jobsPage.title',
    defaultMessage: 'Jobs',
    description: 'Heading of the Jobs page table.',
  },
  descriptionAdmin: {
    id: 'reviewBulk.jobsPage.descriptionAdmin',
    defaultMessage: 'Bulk Skip All Reviews runs by every user.',
    description: "Description under the Jobs heading for an Admin, who sees every user's jobs.",
  },
  descriptionOwn: {
    id: 'reviewBulk.jobsPage.descriptionOwn',
    defaultMessage: 'Your bulk Skip All Reviews runs.',
    description: 'Description under the Jobs heading for a Reviewer, who sees only their own jobs.',
  },
  refresh: {
    id: 'reviewBulk.jobsPage.refresh',
    defaultMessage: 'Refresh jobs',
    description: 'Screen-reader label of the refresh button of the Jobs table.',
  },
  find: {
    id: 'reviewBulk.jobsPage.find',
    defaultMessage: 'Find jobs',
    description: 'Placeholder and screen-reader label of the text filter of the Jobs table.',
  },
  preferences: {
    id: 'reviewBulk.jobsPage.preferences',
    defaultMessage: 'Preferences',
    description: 'Title of the Jobs table settings dialog.',
  },
  confirm: {
    id: 'reviewBulk.jobsPage.confirm',
    defaultMessage: 'Confirm',
    description: 'Confirm button of the Jobs table settings dialog.',
  },
  cancelPreferences: {
    id: 'reviewBulk.jobsPage.cancelPreferences',
    defaultMessage: 'Cancel',
    description: 'Cancel button of the Jobs table settings dialog.',
  },
  pageSize: {
    id: 'reviewBulk.jobsPage.pageSize',
    defaultMessage: 'Page size',
    description: 'Title of the page size choice in the Jobs table settings.',
  },
  pageSizeOption: {
    id: 'reviewBulk.jobsPage.pageSizeOption',
    defaultMessage: '{count, plural, one {# job} other {# jobs}}',
    description: 'A page size option in the Jobs table settings. {count} is the number of jobs per page.',
  },
  columnPreferences: {
    id: 'reviewBulk.jobsPage.columnPreferences',
    defaultMessage: 'Column preferences',
    description: 'Title of the column choice in the Jobs table settings.',
  },
});

/** Jobs are kept for at most 30 days, so a few pages hold all of them. */
const PAGE_LIMIT = 100;
const MAX_PAGES = 10;
/** The page sizes offered in the table's settings. */
const PAGE_SIZES = [10, 20, 50];

export const skippedCount = (job: Pick<BulkReviewJob, 'counts'>): number => job.counts.find((c) => c.outcome === 'SKIPPED')?.count ?? 0;

export const JobStatus = ({ job }: { job: Pick<BulkReviewJob, 'status' | 'stalled'> }): React.JSX.Element => {
  const view = jobStatusView(job.status);
  return (
    <SpaceBetween direction="horizontal" size="xxs">
      <StatusIndicator type={view.type}>{view.label}</StatusIndicator>
      {job.stalled && (
        <StatusIndicator type="warning">
          <FormattedMessage {...messages.stalled} />
        </StatusIndicator>
      )}
    </SpaceBetween>
  );
};

/** The table's columns: id, the message naming it (header and column preference), always shown. */
export const JOB_COLUMN_OPTIONS = [
  { id: 'jobId', label: messages.columnJobId, alwaysVisible: true },
  { id: 'type', label: messages.columnType },
  { id: 'status', label: messages.columnStatus },
  { id: 'mode', label: messages.columnMode },
  { id: 'profile', label: messages.columnProfile },
  { id: 'requestedBy', label: messages.columnRequestedBy },
  { id: 'created', label: messages.columnCreated },
  { id: 'finished', label: messages.columnFinished },
  { id: 'processed', label: messages.columnProcessed },
  { id: 'skipped', label: messages.columnSkipped },
  { id: 'actions', label: messages.columnAction, alwaysVisible: true },
] as const;

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
export const jobMenuItems = (job: Pick<BulkReviewJob, 'status'>, intl: IntlShape): ButtonDropdownProps.Item[] => {
  const active = ACTIVE_JOB_STATUSES.has(job.status);
  return [
    ...(active ? [{ id: 'cancel', text: intl.formatMessage(messages.cancel) }] : []),
    {
      id: 'delete',
      text: intl.formatMessage(messages.delete),
      disabled: active,
      disabledReason: active ? intl.formatMessage(messages.deleteDisabled) : undefined,
    },
  ];
};

const searchText = (j: BulkReviewJob): string =>
  [j.jobId, JOB_TYPE_LABELS[j.jobType] ?? j.jobType, jobStatusView(j.status).label, j.requestedBy, j.profile ?? '', j.reason]
    .join(' ')
    .toLowerCase();

const JobsPage = (): React.JSX.Element => {
  // 1. Hooks
  const intl = useIntl();
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
      setError(describeApiError(e, intl.formatMessage(messages.loadJobsAction)));
    } finally {
      setLoading(false);
    }
  }, [intl]);

  const { items, collectionProps, filterProps, filteredItemsCount, paginationProps, actions } = useCollection(jobs ?? [], {
    filtering: {
      empty: (
        <TableEmptyState
          resourceName={intl.formatMessage(messages.resourceName)}
          title={intl.formatMessage(messages.emptyTitle)}
          description={intl.formatMessage(messages.emptyDescription)}
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
        setSuccessMessage(intl.formatMessage(messages.cancelled, { jobId: job.jobId }));
      } else {
        await deleteBulkJob(job.jobId);
        setJobs((prev) => (prev ?? []).filter((j) => j.jobId !== job.jobId));
        setSuccessMessage(intl.formatMessage(messages.deleted, { jobId: job.jobId }));
      }
    } catch (e) {
      setErrorMessage(describeApiError(e, intl.formatMessage(action === 'cancel' ? messages.cancelJobAction : messages.deleteJobAction)));
      void load();
    } finally {
      setBusy(false);
      closeDialog();
    }
  };

  // 5. Render
  const columnDefinitions: TableProps.ColumnDefinition<BulkReviewJob>[] = [
    {
      id: 'jobId',
      header: intl.formatMessage(messages.columnJobId),
      cell: (j) => <Link href={jobDetailHref(j.jobId)}>{j.jobId}</Link>,
      sortingField: 'jobId',
    },
    {
      id: 'type',
      header: intl.formatMessage(messages.columnType),
      cell: (j) => JOB_TYPE_LABELS[j.jobType] ?? j.jobType,
      sortingField: 'jobType',
    },
    { id: 'status', header: intl.formatMessage(messages.columnStatus), cell: (j) => <JobStatus job={j} />, sortingField: 'status' },
    { id: 'mode', header: intl.formatMessage(messages.columnMode), cell: (j) => JOB_MODE_LABELS[j.mode] ?? j.mode, sortingField: 'mode' },
    { id: 'profile', header: intl.formatMessage(messages.columnProfile), cell: (j) => j.profile ?? '-', sortingField: 'profile' },
    { id: 'requestedBy', header: intl.formatMessage(messages.columnRequestedBy), cell: (j) => j.requestedBy, sortingField: 'requestedBy' },
    {
      id: 'created',
      header: intl.formatMessage(messages.columnCreated),
      cell: (j) => formatDateTime(j.createdAt),
      sortingField: 'createdAt',
    },
    {
      id: 'finished',
      header: intl.formatMessage(messages.columnFinished),
      cell: (j) => (j.finishedAt ? formatDateTime(j.finishedAt) : '-'),
      sortingField: 'finishedAt',
    },
    {
      id: 'processed',
      header: intl.formatMessage(messages.columnProcessed),
      cell: (j) => intl.formatMessage(messages.processedOfTotal, { processed: j.processed, total: j.total }),
    },
    { id: 'skipped', header: intl.formatMessage(messages.columnSkipped), cell: (j) => skippedCount(j).toLocaleString() },
    {
      id: 'actions',
      header: intl.formatMessage(messages.columnAction),
      cell: (j) => (
        <ButtonDropdown
          variant="inline-icon"
          ariaLabel={intl.formatMessage(messages.actionsFor, { jobId: j.jobId })}
          expandToViewport
          disabled={busy}
          items={jobMenuItems(j, intl)}
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
          header={intl.formatMessage(messages.loadFailed)}
          action={
            <Button onClick={() => void load()} loading={loading}>
              <FormattedMessage {...messages.retry} />
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
        loadingText={intl.formatMessage(messages.loading)}
        header={
          <TableHeader
            title={
              <span ref={headingRef} tabIndex={-1}>
                <FormattedMessage {...messages.title} />
              </span>
            }
            totalItems={jobs ?? []}
            description={intl.formatMessage(isAdmin ? messages.descriptionAdmin : messages.descriptionOwn)}
            actionButtons={
              <Button iconName="refresh" ariaLabel={intl.formatMessage(messages.refresh)} onClick={() => void load()} loading={loading} />
            }
          />
        }
        filter={
          <TextFilter
            {...filterProps}
            filteringPlaceholder={intl.formatMessage(messages.find)}
            filteringAriaLabel={intl.formatMessage(messages.find)}
            countText={getFilterCounterText(filteredItemsCount ?? 0)}
          />
        }
        pagination={<Pagination {...paginationProps} ariaLabels={paginationLabels} />}
        preferences={
          <CollectionPreferences
            title={intl.formatMessage(messages.preferences)}
            confirmLabel={intl.formatMessage(messages.confirm)}
            cancelLabel={intl.formatMessage(messages.cancelPreferences)}
            preferences={preferences}
            onConfirm={({ detail }) =>
              setPreferences({
                pageSize: detail.pageSize ?? DEFAULT_JOBS_PREFERENCES.pageSize,
                contentDisplay: [...(detail.contentDisplay ?? DEFAULT_JOBS_PREFERENCES.contentDisplay)],
              })
            }
            pageSizePreference={{
              title: intl.formatMessage(messages.pageSize),
              options: PAGE_SIZES.map((count) => ({ value: count, label: intl.formatMessage(messages.pageSizeOption, { count }) })),
            }}
            contentDisplayPreference={{
              title: intl.formatMessage(messages.columnPreferences),
              options: JOB_COLUMN_OPTIONS.map((c) => ({ ...c, label: intl.formatMessage(c.label) })),
            }}
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
