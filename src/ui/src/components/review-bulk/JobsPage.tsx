// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Home › Jobs: bulk Skip All Reviews runs, newest first. A Reviewer sees their own;
// an Admin sees every job.
import React, { useCallback, useEffect, useState } from 'react';
import { Box, Button, Container, Header, Link, SpaceBetween, StatusIndicator, Table } from '@cloudscape-design/components';

import useAppContext from '../../contexts/app';
import usePolling from '../../hooks/use-polling';
import useUserRole from '../../hooks/use-user-role';
import { describeApiError } from '../../hooks/utils/graphql-error';
import { jobDetailHref } from '../../routes/constants';
import { formatDateTime } from '../../utils/date-time';
import type { BulkReviewJob } from '../../graphql/generated/schema-types';
import { listBulkJobs } from './bulk-review-api';
import { JOB_MODE_LABELS, JOB_TYPE_LABELS, jobStatusView } from './bulk-outcomes';

const ACTIVE = new Set(['QUEUED', 'RUNNING']);

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

const JobsPage = (): React.JSX.Element => {
  const { setErrorMessage } = useAppContext();
  const { isAdmin } = useUserRole();
  const [jobs, setJobs] = useState<BulkReviewJob[] | null>(null);
  const [nextToken, setNextToken] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const page = await listBulkJobs({ limit: 25 });
      setJobs(page.jobs);
      setNextToken(page.nextToken ?? null);
      setError(null);
    } catch (e) {
      setError(describeApiError(e, 'load jobs'));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);
  usePolling(() => void load(), { enabled: !!jobs?.some((j) => ACTIVE.has(j.status)), intervalMs: 10_000 });

  const loadMore = async () => {
    if (!nextToken) return;
    setLoadingMore(true);
    try {
      const page = await listBulkJobs({ limit: 25, nextToken });
      setJobs((prev) => [...(prev ?? []), ...page.jobs]);
      setNextToken(page.nextToken ?? null);
    } catch (e) {
      setErrorMessage(describeApiError(e, 'load more jobs'));
    } finally {
      setLoadingMore(false);
    }
  };

  return (
    <Container
      header={
        <Header
          variant="h1"
          description={isAdmin ? 'Bulk Skip All Reviews runs by every user.' : 'Your bulk Skip All Reviews runs.'}
          actions={<Button iconName="refresh" ariaLabel="Refresh" onClick={() => void load()} />}
        >
          Jobs
        </Header>
      }
    >
      <Table
        variant="embedded"
        items={jobs ?? []}
        loading={jobs === null && !error}
        loadingText="Loading jobs"
        empty={
          error ? (
            <Box color="text-status-error">{error}</Box>
          ) : (
            <Box textAlign="center" color="inherit">
              <b>No jobs yet</b>
              <Box variant="p" color="inherit">
                Bulk Skip All Reviews runs appear here.
              </Box>
            </Box>
          )
        }
        columnDefinitions={[
          { id: 'jobId', header: 'Job ID', cell: (j) => <Link href={jobDetailHref(j.jobId)}>{j.jobId}</Link> },
          { id: 'type', header: 'Type', cell: (j) => JOB_TYPE_LABELS[j.jobType] ?? j.jobType },
          { id: 'status', header: 'Status', cell: (j) => <JobStatus job={j} /> },
          { id: 'mode', header: 'Mode', cell: (j) => JOB_MODE_LABELS[j.mode] ?? j.mode },
          { id: 'profile', header: 'Profile', cell: (j) => j.profile ?? '-' },
          ...(isAdmin ? [{ id: 'requestedBy', header: 'Requested by', cell: (j: BulkReviewJob) => j.requestedBy }] : []),
          { id: 'created', header: 'Created', cell: (j) => formatDateTime(j.createdAt) },
          { id: 'finished', header: 'Finished', cell: (j) => (j.finishedAt ? formatDateTime(j.finishedAt) : '-') },
          { id: 'processed', header: 'Processed / Total', cell: (j) => `${j.processed.toLocaleString()} / ${j.total.toLocaleString()}` },
          { id: 'skipped', header: 'Skipped', cell: (j) => skippedCount(j).toLocaleString() },
        ]}
        footer={
          nextToken ? (
            <Box textAlign="center">
              <Button loading={loadingMore} onClick={() => void loadMore()}>
                Load more
              </Button>
            </Box>
          ) : undefined
        }
      />
    </Container>
  );
};

export default JobsPage;
