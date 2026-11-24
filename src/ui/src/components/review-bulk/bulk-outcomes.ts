// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The per-document outcomes of bulk Skip All Reviews and Send to review. The server
// list is canonical (`BulkOutcome` in
// lib/idp_common_pkg/idp_common/review_queues/bulk_models.py); bulk-outcomes.test.ts
// asserts the same values in the same order.
import type { StatusIndicatorProps } from '@cloudscape-design/components';
import { defineMessages, type IntlShape, type NoMessageValues, type TypedMessageDescriptor } from 'react-intl';

export interface OutcomeView {
  label: string;
  type: StatusIndicatorProps.Type;
}

/** The label in the UI's language with `intl`; without it, the English source text. */
const labelOf = (message: TypedMessageDescriptor<NoMessageValues>, intl?: IntlShape): string =>
  intl ? intl.formatMessage(message) : String(message.defaultMessage);

/** Whether `map` has an entry of its own for `code` (so `toString` is not a code). */
const hasCode = <K extends string>(map: Readonly<Record<K, unknown>>, code: string): code is K => Object.hasOwn(map, code);

type BulkOutcome =
  | 'SKIPPED'
  | 'SENT'
  | 'NOT_PERMITTED'
  | 'NOT_PENDING'
  | 'ALREADY_FINISHED'
  | 'HELD_BY_OTHER'
  | 'TEST_STUDIO_EXCLUDED'
  | 'NOT_FINISHED_PROCESSING'
  | 'ALREADY_IN_REVIEW'
  | 'REVIEW_QUEUE_OFF'
  | 'RESUBMIT_FAILED'
  | 'FAILED'
  | 'NOT_ATTEMPTED';

/** The StatusIndicator type of each outcome, in the server's order (Cloudscape's names, not text). */
export const BULK_OUTCOMES: Record<BulkOutcome, StatusIndicatorProps.Type> = {
  SKIPPED: 'success',
  SENT: 'success',
  NOT_PERMITTED: 'stopped',
  NOT_PENDING: 'stopped',
  ALREADY_FINISHED: 'stopped',
  HELD_BY_OTHER: 'warning',
  TEST_STUDIO_EXCLUDED: 'stopped',
  NOT_FINISHED_PROCESSING: 'stopped',
  ALREADY_IN_REVIEW: 'stopped',
  REVIEW_QUEUE_OFF: 'stopped',
  RESUBMIT_FAILED: 'warning',
  FAILED: 'error',
  NOT_ATTEMPTED: 'pending',
};

/** How each outcome reads, keyed by the code the server returns. */
const outcomeMessages = defineMessages<Record<BulkOutcome, NoMessageValues>>({
  SKIPPED: {
    id: 'reviewBulk.outcomes.outcome.skipped',
    defaultMessage: 'Skipped',
    description: 'Outcome of one document in bulk Skip All Reviews: its review was skipped.',
  },
  SENT: {
    id: 'reviewBulk.outcomes.outcome.sent',
    defaultMessage: 'Sent',
    description: 'Outcome of one document in Send to review: it was sent to the Default review queue.',
  },
  NOT_PERMITTED: {
    id: 'reviewBulk.outcomes.outcome.notPermitted',
    defaultMessage: 'Not permitted',
    description:
      "Outcome of one document in bulk Skip All Reviews or Send to review: left alone, because it is outside the user's scope or does not exist.",
  },
  NOT_PENDING: {
    id: 'reviewBulk.outcomes.outcome.notPending',
    defaultMessage: 'Not pending',
    description: 'Outcome of one document in bulk Skip All Reviews: left alone, because it has no pending review.',
  },
  ALREADY_FINISHED: {
    id: 'reviewBulk.outcomes.outcome.alreadyFinished',
    defaultMessage: 'Already finished',
    description: 'Outcome of one document in bulk Skip All Reviews: left alone, because its review is already finished.',
  },
  HELD_BY_OTHER: {
    id: 'reviewBulk.outcomes.outcome.heldByOther',
    defaultMessage: 'Held by another reviewer',
    description: 'Outcome of one document in bulk Skip All Reviews: left alone, because another reviewer holds its review.',
  },
  TEST_STUDIO_EXCLUDED: {
    id: 'reviewBulk.outcomes.outcome.testStudioExcluded',
    defaultMessage: 'Test Studio excluded',
    description: 'Outcome of one document in bulk Skip All Reviews or Send to review: left alone, because it is a Test Studio document.',
  },
  NOT_FINISHED_PROCESSING: {
    id: 'reviewBulk.outcomes.outcome.notFinishedProcessing',
    defaultMessage: 'Not finished processing',
    description: 'Outcome of one document in bulk Skip All Reviews or Send to review: left alone, because it has not finished processing.',
  },
  ALREADY_IN_REVIEW: {
    id: 'reviewBulk.outcomes.outcome.alreadyInReview',
    defaultMessage: 'Already in review',
    description: 'Outcome of one document in Send to review: not sent, because it is already in a review.',
  },
  REVIEW_QUEUE_OFF: {
    id: 'reviewBulk.outcomes.outcome.reviewQueueOff',
    defaultMessage: 'Review queue off',
    description: 'Outcome of one document in Send to review: not sent, because the Default review queue is turned off.',
  },
  RESUBMIT_FAILED: {
    id: 'reviewBulk.outcomes.outcome.resubmitFailed',
    defaultMessage: 'Resubmit failed',
    description:
      'Outcome of one document in bulk Skip All Reviews: its review was skipped, but its downstream processing run could not be queued.',
  },
  FAILED: {
    id: 'reviewBulk.outcomes.outcome.failed',
    defaultMessage: 'Failed',
    description: 'Outcome of one document in bulk Skip All Reviews or Send to review: an error stopped it, and it can be tried again.',
  },
  NOT_ATTEMPTED: {
    id: 'reviewBulk.outcomes.outcome.notAttempted',
    defaultMessage: 'Not attempted',
    description:
      'Outcome of one document in bulk Skip All Reviews: not tried, because the run reached its time limit first; it can be tried again.',
  },
});

/**
 * An outcome's label and StatusIndicator type. The label is in the UI's language with `intl`;
 * without it, the English source text. An outcome the UI does not know is shown as it is.
 */
export const outcomeView = (outcome: string, intl?: IntlShape): OutcomeView =>
  hasCode(BULK_OUTCOMES, outcome)
    ? { label: labelOf(outcomeMessages[outcome], intl), type: BULK_OUTCOMES[outcome] }
    : { label: outcome, type: 'info' };

type DetailCode = 'finalize_retry_pending' | 'resubmit_skipped' | 'resubmit_failed' | 'resubmitter_not_configured';

/** Detail codes the server attaches to an outcome, as people read them. */
const detailMessages = defineMessages<Record<DetailCode, NoMessageValues>>({
  finalize_retry_pending: {
    id: 'reviewBulk.outcomes.detail.finalizeRetryPending',
    defaultMessage: 'Finishing run will be retried',
    description:
      'Detail of a skipped review queue item in the bulk results table: the run that finishes its review could not start at once and is retried automatically.',
  },
  resubmit_skipped: {
    id: 'reviewBulk.outcomes.detail.resubmitSkipped',
    defaultMessage: 'Not resubmitted (by request)',
    description:
      'Detail of a skipped document in the bulk results table: it was not resubmitted for downstream processing, as the requester chose.',
  },
  resubmit_failed: {
    id: 'reviewBulk.outcomes.detail.resubmitFailed',
    defaultMessage: 'Resubmit failed',
    description: 'Detail of a skipped document in the bulk results table: resubmitting it for downstream processing failed.',
  },
  resubmitter_not_configured: {
    id: 'reviewBulk.outcomes.detail.resubmitterNotConfigured',
    defaultMessage: 'Resubmit not configured',
    description: 'Detail of a skipped document in the bulk results table: it was not resubmitted, because resubmitting is not set up.',
  },
});

/** A detail code as people read it, in the UI's language with `intl`; an unknown code as it is, none as ''. */
export const detailLabel = (detail?: string | null, intl?: IntlShape): string =>
  detail ? (hasCode(detailMessages, detail) ? labelOf(detailMessages[detail], intl) : detail) : '';

type JobStatusValue = 'QUEUED' | 'RUNNING' | 'SUCCEEDED' | 'FAILED' | 'CANCELLED';

/** The StatusIndicator type of each job status (Cloudscape's names, not text). */
export const JOB_STATUSES: Record<JobStatusValue, StatusIndicatorProps.Type> = {
  QUEUED: 'pending',
  RUNNING: 'in-progress',
  SUCCEEDED: 'success',
  FAILED: 'error',
  CANCELLED: 'stopped',
};

/** Job statuses, as the Jobs page shows them. */
const jobStatusMessages = defineMessages<Record<JobStatusValue, NoMessageValues>>({
  QUEUED: {
    id: 'reviewBulk.outcomes.jobStatus.queued',
    defaultMessage: 'Queued',
    description: "Status of a bulk Skip All Reviews job that is waiting to start. Shown in the Jobs table and on the job's page.",
  },
  RUNNING: {
    id: 'reviewBulk.outcomes.jobStatus.running',
    defaultMessage: 'Running',
    description: "Status of a bulk Skip All Reviews job that is working. Shown in the Jobs table and on the job's page.",
  },
  SUCCEEDED: {
    id: 'reviewBulk.outcomes.jobStatus.succeeded',
    defaultMessage: 'Succeeded',
    description: "Status of a bulk Skip All Reviews job that finished. Shown in the Jobs table and on the job's page.",
  },
  FAILED: {
    id: 'reviewBulk.outcomes.jobStatus.failed',
    defaultMessage: 'Failed',
    description: "Status of a bulk Skip All Reviews job that stopped on an error. Shown in the Jobs table and on the job's page.",
  },
  CANCELLED: {
    id: 'reviewBulk.outcomes.jobStatus.cancelled',
    defaultMessage: 'Cancelled',
    description:
      "Status of a bulk Skip All Reviews job stopped at the requester's or an Admin's request. Shown in the Jobs table and on the job's page.",
  },
});

/** A job that is still waiting or working, and so can be cancelled (and not deleted). */
export const ACTIVE_JOB_STATUSES: ReadonlySet<string> = new Set(['QUEUED', 'RUNNING']);

/** A job status's label and StatusIndicator type, worded as `outcomeView` words an outcome. */
export const jobStatusView = (status: string, intl?: IntlShape): OutcomeView =>
  hasCode(JOB_STATUSES, status)
    ? { label: labelOf(jobStatusMessages[status], intl), type: JOB_STATUSES[status] }
    : { label: status, type: 'info' };

const jobTypeMessages = defineMessages<{ SKIP_ALL_REVIEWS: NoMessageValues }>({
  SKIP_ALL_REVIEWS: {
    id: 'reviewBulk.outcomes.jobType.skipAllReviews',
    defaultMessage: 'Skip All Reviews',
    description: "Type of a bulk Skip All Reviews job (the action's name), in the Jobs table and as the heading of the job's page.",
  },
});

const jobModeMessages = defineMessages<{ SELECTED: NoMessageValues; PROFILE: NoMessageValues }>({
  SELECTED: {
    id: 'reviewBulk.outcomes.jobMode.selected',
    defaultMessage: 'Selected documents',
    description:
      "How a bulk Skip All Reviews job chose its documents: the ones the user selected. Shown in the Jobs table and on the job's page.",
  },
  PROFILE: {
    id: 'reviewBulk.outcomes.jobMode.profile',
    defaultMessage: 'By profile',
    description:
      "How a bulk Skip All Reviews job chose its documents: by configuration profile and submitted-date range. Shown in the Jobs table and on the job's page.",
  },
});

/** A job type as people read it, in the UI's language with `intl`; a type the UI does not know as it is. */
export const jobTypeLabel = (jobType: string, intl?: IntlShape): string =>
  hasCode(jobTypeMessages, jobType) ? labelOf(jobTypeMessages[jobType], intl) : jobType;

/** How a job chose its documents, in the UI's language with `intl`; a mode the UI does not know as it is. */
export const jobModeLabel = (mode: string, intl?: IntlShape): string =>
  hasCode(jobModeMessages, mode) ? labelOf(jobModeMessages[mode], intl) : mode;
