// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// One bulk Skip All Reviews job: what was asked, progress, counts and per-document
// results. Polled while the job is queued or running.
import React, { useCallback, useEffect, useState } from 'react';
import { useParams } from 'react-router-dom';
import { Alert, Box, Button, Container, Header, KeyValuePairs, ProgressBar, SpaceBetween, Spinner } from '@cloudscape-design/components';

import usePolling from '../../hooks/use-polling';
import { describeApiError, isAuthorizationError } from '../../hooks/utils/graphql-error';
import { formatDateTime } from '../../utils/date-time';
import type { BulkDocumentOutcome, BulkReviewJob } from '../../graphql/generated/schema-types';
import { getBulkJob } from './bulk-review-api';
import { JOB_MODE_LABELS, JOB_TYPE_LABELS } from './bulk-outcomes';
import BulkResultsTable from './BulkResultsTable';
import { JobStatus } from './JobsPage';
import OutcomeCounts from './OutcomeCounts';

const ACTIVE = new Set(['QUEUED', 'RUNNING']);

const isNotFound = (e: unknown): boolean =>
  /NOT_FOUND|not found/i.test(String((e as { errors?: { message?: string }[] })?.errors?.[0]?.message ?? e));

const JobDetailPage = (): React.JSX.Element => {
  const { jobId = '' } = useParams();
  const [job, setJob] = useState<BulkReviewJob | null>(null);
  const [results, setResults] = useState<BulkDocumentOutcome[]>([]);
  const [nextToken, setNextToken] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);

  const load = useCallback(async () => {
    try {
      const page = await getBulkJob({ jobId, limit: 100 });
      setJob(page.job);
      setResults(page.results);
      setNextToken(page.nextToken ?? null);
      setError(null);
    } catch (e) {
      setError(isNotFound(e) || isAuthorizationError(e) ? 'Job not found, or not yours.' : describeApiError(e, 'load the job'));
    }
  }, [jobId]);

  useEffect(() => {
    void load();
  }, [load]);
  usePolling(() => void load(), { enabled: !!job && ACTIVE.has(job.status), intervalMs: 5_000 });

  const loadMore = async () => {
    if (!nextToken) return;
    setLoadingMore(true);
    try {
      const page = await getBulkJob({ jobId, limit: 100, nextToken });
      setResults((prev) => [...prev, ...page.results]);
      setNextToken(page.nextToken ?? null);
    } catch (e) {
      setError(describeApiError(e, 'load more results'));
    } finally {
      setLoadingMore(false);
    }
  };

  if (error && !job) return <Alert type="error">{error}</Alert>;
  if (!job) return <Spinner size="large" />;
  const pct = job.total > 0 ? Math.min(100, Math.round((job.processed / job.total) * 100)) : ACTIVE.has(job.status) ? 0 : 100;

  return (
    <SpaceBetween size="l">
      <Container
        header={
          <Header variant="h1" description={job.jobId}>
            {JOB_TYPE_LABELS[job.jobType] ?? job.jobType}
          </Header>
        }
      >
        <SpaceBetween size="m">
          {error && <Alert type="error">{error}</Alert>}
          {job.stalled && <Alert type="warning">This job has not reported progress for 30 minutes.</Alert>}
          {job.limitReached && (
            <Alert type="warning">
              More than 10,000 documents matched; only the first 10,000 were processed. Narrow the date range and run again.
            </Alert>
          )}
          {job.status === 'FAILED' && (
            <Alert type="error" header="The job failed">
              {job.error ?? 'Unknown error'}
            </Alert>
          )}
          <KeyValuePairs
            columns={3}
            items={[
              { label: 'Status', value: <JobStatus job={job} /> },
              { label: 'Mode', value: JOB_MODE_LABELS[job.mode] ?? job.mode },
              { label: 'Profile', value: job.profile ?? '-' },
              {
                label: 'Submitted between (UTC)',
                value: job.submittedFrom && job.submittedTo ? `${job.submittedFrom} → ${job.submittedTo}` : '-',
              },
              { label: 'Reason', value: job.reason },
              { label: 'Note', value: job.note || '-' },
              { label: 'Resubmit for downstream processing', value: job.resubmit ? 'Yes' : 'No' },
              { label: 'Requested by', value: job.requestedBy },
              { label: 'Created', value: formatDateTime(job.createdAt) },
              { label: 'Finished', value: job.finishedAt ? formatDateTime(job.finishedAt) : '-' },
            ]}
          />
          <ProgressBar
            value={pct}
            label="Progress"
            description={`${job.processed.toLocaleString()} of ${job.total.toLocaleString()} documents processed`}
            status={job.status === 'FAILED' ? 'error' : job.status === 'SUCCEEDED' ? 'success' : 'in-progress'}
            resultText={job.status === 'SUCCEEDED' ? 'Finished' : (job.error ?? 'Failed')}
          />
          <OutcomeCounts counts={job.counts} />
        </SpaceBetween>
      </Container>
      <Container>
        <BulkResultsTable
          results={results}
          exportName={`skip-all-reviews-${job.jobId}`}
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
    </SpaceBetween>
  );
};

export default JobDetailPage;
