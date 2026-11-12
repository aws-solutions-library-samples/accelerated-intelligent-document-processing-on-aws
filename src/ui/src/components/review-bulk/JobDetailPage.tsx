// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// One bulk Skip All Reviews job: what was asked, progress, counts and per-document
// results. Polled while the job is queued or running.
import React, { useCallback, useEffect, useState } from 'react';
import { useParams } from 'react-router-dom';
import { Alert, Box, Button, Container, Header, KeyValuePairs, ProgressBar, SpaceBetween, Spinner } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, useIntl, type NoMessageValues } from 'react-intl';

import usePolling from '../../hooks/use-polling';
import { describeApiError, isAuthorizationError } from '../../hooks/utils/graphql-error';
import { formatDateTime } from '../../utils/date-time';
import type { BulkDocumentOutcome, BulkReviewJob } from '../../graphql/generated/schema-types';
import { getBulkJob } from './bulk-review-api';
import { ACTIVE_JOB_STATUSES, JOB_MODE_LABELS, JOB_TYPE_LABELS } from './bulk-outcomes';
import BulkResultsTable from './BulkResultsTable';
import { JobStatus } from './JobsPage';
import OutcomeCounts from './OutcomeCounts';

const messages = defineMessages<{
  notFound: NoMessageValues;
  loadJobAction: NoMessageValues;
  loadMoreAction: NoMessageValues;
  stalled: NoMessageValues;
  limitReached: NoMessageValues;
  failedHeader: NoMessageValues;
  unknownError: NoMessageValues;
  status: NoMessageValues;
  mode: NoMessageValues;
  profile: NoMessageValues;
  submittedBetween: NoMessageValues;
  submittedRange: { readonly from: string; readonly to: string };
  reason: NoMessageValues;
  note: NoMessageValues;
  resubmit: NoMessageValues;
  yes: NoMessageValues;
  no: NoMessageValues;
  requestedBy: NoMessageValues;
  created: NoMessageValues;
  finished: NoMessageValues;
  progress: NoMessageValues;
  progressDescription: { readonly processed: number; readonly total: number };
  resultFinished: NoMessageValues;
  resultCancelled: NoMessageValues;
  resultFailed: NoMessageValues;
  loadMore: NoMessageValues;
}>({
  notFound: {
    id: 'reviewBulk.jobDetailPage.notFound',
    defaultMessage: 'Job not found, or not yours.',
    description: 'Error on the job detail page when the job does not exist or belongs to another user.',
  },
  loadJobAction: {
    id: 'reviewBulk.jobDetailPage.loadJobAction',
    defaultMessage: 'load the job',
    description:
      'Action phrase completing an API error sentence ("Failed to …: <error>" / "You do not have permission to …") when the job detail cannot be loaded.',
  },
  loadMoreAction: {
    id: 'reviewBulk.jobDetailPage.loadMoreAction',
    defaultMessage: 'load more results',
    description:
      'Action phrase completing an API error sentence ("Failed to …: <error>" / "You do not have permission to …") when more job results cannot be loaded.',
  },
  stalled: {
    id: 'reviewBulk.jobDetailPage.stalled',
    defaultMessage: 'This job has not reported progress for 30 minutes.',
    description: 'Warning on the job detail page when a running job looks stuck.',
  },
  limitReached: {
    id: 'reviewBulk.jobDetailPage.limitReached',
    defaultMessage: 'More than 10,000 documents matched; only the first 10,000 were processed. Narrow the date range and run again.',
    description: 'Warning on the job detail page when the job reached the limit of 10,000 documents.',
  },
  failedHeader: {
    id: 'reviewBulk.jobDetailPage.failedHeader',
    defaultMessage: 'The job failed',
    description: 'Header of the error alert on the job detail page of a failed job; the body is the error.',
  },
  unknownError: {
    id: 'reviewBulk.jobDetailPage.unknownError',
    defaultMessage: 'Unknown error',
    description: 'Shown as the error of a failed job when the server recorded none.',
  },
  status: {
    id: 'reviewBulk.jobDetailPage.status',
    defaultMessage: 'Status',
    description: 'Label of the job status on the job detail page.',
  },
  mode: {
    id: 'reviewBulk.jobDetailPage.mode',
    defaultMessage: 'Mode',
    description: 'Label of how the job chose documents (selected documents or by profile) on the job detail page.',
  },
  profile: {
    id: 'reviewBulk.jobDetailPage.profile',
    defaultMessage: 'Profile',
    description: 'Label of the configuration profile of the job on the job detail page.',
  },
  submittedBetween: {
    id: 'reviewBulk.jobDetailPage.submittedBetween',
    defaultMessage: 'Submitted between (UTC)',
    description: 'Label of the submitted-date range the job applied to, in UTC, on the job detail page.',
  },
  submittedRange: {
    id: 'reviewBulk.jobDetailPage.submittedRange',
    defaultMessage: '{from} → {to}',
    description: 'The submitted-date range of a job. {from} and {to} are UTC ISO timestamps.',
  },
  reason: {
    id: 'reviewBulk.jobDetailPage.reason',
    defaultMessage: 'Reason',
    description: 'Label of the reason the requester gave for skipping the reviews, on the job detail page.',
  },
  note: {
    id: 'reviewBulk.jobDetailPage.note',
    defaultMessage: 'Note',
    description: 'Label of the requester note on the job detail page.',
  },
  resubmit: {
    id: 'reviewBulk.jobDetailPage.resubmit',
    defaultMessage: 'Resubmit for downstream processing',
    description: 'Label of whether skipped documents are resubmitted for downstream processing, on the job detail page.',
  },
  yes: {
    id: 'reviewBulk.jobDetailPage.yes',
    defaultMessage: 'Yes',
    description: 'Value of the Resubmit for downstream processing field: resubmitted.',
  },
  no: {
    id: 'reviewBulk.jobDetailPage.no',
    defaultMessage: 'No',
    description: 'Value of the Resubmit for downstream processing field: not resubmitted.',
  },
  requestedBy: {
    id: 'reviewBulk.jobDetailPage.requestedBy',
    defaultMessage: 'Requested by',
    description: 'Label of the user who started the job, on the job detail page.',
  },
  created: {
    id: 'reviewBulk.jobDetailPage.created',
    defaultMessage: 'Created',
    description: 'Label of the date and time the job was created, on the job detail page.',
  },
  finished: {
    id: 'reviewBulk.jobDetailPage.finished',
    defaultMessage: 'Finished',
    description: 'Label of the date and time the job finished, on the job detail page.',
  },
  progress: {
    id: 'reviewBulk.jobDetailPage.progress',
    defaultMessage: 'Progress',
    description: 'Label of the progress bar on the job detail page.',
  },
  progressDescription: {
    id: 'reviewBulk.jobDetailPage.progressDescription',
    defaultMessage: '{processed, number} of {total, number} documents processed',
    description: 'Under the job progress bar. {processed} is the number of documents done so far, {total} the number the job applies to.',
  },
  resultFinished: {
    id: 'reviewBulk.jobDetailPage.resultFinished',
    defaultMessage: 'Finished',
    description: 'Result text of the job progress bar when the job succeeded.',
  },
  resultCancelled: {
    id: 'reviewBulk.jobDetailPage.resultCancelled',
    defaultMessage: 'Cancelled',
    description: 'Result text of the job progress bar when the job was cancelled.',
  },
  resultFailed: {
    id: 'reviewBulk.jobDetailPage.resultFailed',
    defaultMessage: 'Failed',
    description: 'Result text of the job progress bar when the job failed and the server recorded no error.',
  },
  loadMore: {
    id: 'reviewBulk.jobDetailPage.loadMore',
    defaultMessage: 'Load more',
    description: 'Button under the job results table that loads the next page of per-document results.',
  },
});

/** A cancelled job ends like a failed one: stopped before it finished. */
const progressStatus = (status: string): 'error' | 'success' | 'in-progress' =>
  status === 'SUCCEEDED' ? 'success' : status === 'FAILED' || status === 'CANCELLED' ? 'error' : 'in-progress';

const isNotFound = (e: unknown): boolean =>
  /NOT_FOUND|not found/i.test(String((e as { errors?: { message?: string }[] })?.errors?.[0]?.message ?? e));

const JobDetailPage = (): React.JSX.Element => {
  const intl = useIntl();
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
      setError(
        isNotFound(e) || isAuthorizationError(e)
          ? intl.formatMessage(messages.notFound)
          : describeApiError(e, intl.formatMessage(messages.loadJobAction)),
      );
    }
  }, [jobId, intl]);

  useEffect(() => {
    void load();
  }, [load]);
  usePolling(() => void load(), { enabled: !!job && ACTIVE_JOB_STATUSES.has(job.status), intervalMs: 5_000 });

  const loadMore = async () => {
    if (!nextToken) return;
    setLoadingMore(true);
    try {
      const page = await getBulkJob({ jobId, limit: 100, nextToken });
      setResults((prev) => [...prev, ...page.results]);
      setNextToken(page.nextToken ?? null);
    } catch (e) {
      setError(describeApiError(e, intl.formatMessage(messages.loadMoreAction)));
    } finally {
      setLoadingMore(false);
    }
  };

  if (error && !job) return <Alert type="error">{error}</Alert>;
  if (!job) return <Spinner size="large" />;
  const pct = job.total > 0 ? Math.min(100, Math.round((job.processed / job.total) * 100)) : ACTIVE_JOB_STATUSES.has(job.status) ? 0 : 100;

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
          {job.stalled && (
            <Alert type="warning">
              <FormattedMessage {...messages.stalled} />
            </Alert>
          )}
          {job.limitReached && (
            <Alert type="warning">
              <FormattedMessage {...messages.limitReached} />
            </Alert>
          )}
          {job.status === 'FAILED' && (
            <Alert type="error" header={intl.formatMessage(messages.failedHeader)}>
              {job.error ?? intl.formatMessage(messages.unknownError)}
            </Alert>
          )}
          <KeyValuePairs
            columns={3}
            items={[
              { label: intl.formatMessage(messages.status), value: <JobStatus job={job} /> },
              { label: intl.formatMessage(messages.mode), value: JOB_MODE_LABELS[job.mode] ?? job.mode },
              { label: intl.formatMessage(messages.profile), value: job.profile ?? '-' },
              {
                label: intl.formatMessage(messages.submittedBetween),
                value:
                  job.submittedFrom && job.submittedTo
                    ? intl.formatMessage(messages.submittedRange, { from: job.submittedFrom, to: job.submittedTo })
                    : '-',
              },
              { label: intl.formatMessage(messages.reason), value: job.reason },
              { label: intl.formatMessage(messages.note), value: job.note || '-' },
              {
                label: intl.formatMessage(messages.resubmit),
                value: intl.formatMessage(job.resubmit ? messages.yes : messages.no),
              },
              { label: intl.formatMessage(messages.requestedBy), value: job.requestedBy },
              { label: intl.formatMessage(messages.created), value: formatDateTime(job.createdAt) },
              { label: intl.formatMessage(messages.finished), value: job.finishedAt ? formatDateTime(job.finishedAt) : '-' },
            ]}
          />
          <ProgressBar
            value={pct}
            label={intl.formatMessage(messages.progress)}
            description={intl.formatMessage(messages.progressDescription, { processed: job.processed, total: job.total })}
            status={progressStatus(job.status)}
            resultText={
              job.status === 'SUCCEEDED'
                ? intl.formatMessage(messages.resultFinished)
                : job.status === 'CANCELLED'
                  ? intl.formatMessage(messages.resultCancelled)
                  : (job.error ?? intl.formatMessage(messages.resultFailed))
            }
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
                  <FormattedMessage {...messages.loadMore} />
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
