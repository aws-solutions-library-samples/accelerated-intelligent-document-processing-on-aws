// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The per-document outcomes of bulk Skip All Reviews and Send to review. The server
// list is canonical (`BulkOutcome` in
// lib/idp_common_pkg/idp_common/review_queues/bulk_models.py); bulk-outcomes.test.ts
// asserts the same values in the same order.
import type { StatusIndicatorProps } from '@cloudscape-design/components';

export interface OutcomeView {
  label: string;
  type: StatusIndicatorProps.Type;
}

export const BULK_OUTCOMES: Record<string, OutcomeView> = {
  SKIPPED: { label: 'Skipped', type: 'success' },
  SENT: { label: 'Sent', type: 'success' },
  NOT_PERMITTED: { label: 'Not permitted', type: 'stopped' },
  NOT_PENDING: { label: 'Not pending', type: 'stopped' },
  ALREADY_FINISHED: { label: 'Already finished', type: 'stopped' },
  HELD_BY_OTHER: { label: 'Held by another reviewer', type: 'warning' },
  TEST_STUDIO_EXCLUDED: { label: 'Test Studio excluded', type: 'stopped' },
  NOT_FINISHED_PROCESSING: { label: 'Not finished processing', type: 'stopped' },
  ALREADY_IN_REVIEW: { label: 'Already in review', type: 'stopped' },
  REVIEW_QUEUE_OFF: { label: 'Review queue off', type: 'stopped' },
  RESUBMIT_FAILED: { label: 'Resubmit failed', type: 'warning' },
  FAILED: { label: 'Failed', type: 'error' },
  NOT_ATTEMPTED: { label: 'Not attempted', type: 'pending' },
};

export const outcomeView = (outcome: string): OutcomeView => BULK_OUTCOMES[outcome] ?? { label: outcome, type: 'info' };

/** Detail codes the server attaches to an outcome, as people read them. */
export const DETAIL_LABELS: Record<string, string> = {
  finalize_retry_pending: 'Finishing run will be retried',
  resubmit_skipped: 'Not resubmitted (by request)',
  resubmit_failed: 'Resubmit failed',
  resubmitter_not_configured: 'Resubmit not configured',
};

export const detailLabel = (detail?: string | null): string => (detail ? (DETAIL_LABELS[detail] ?? detail) : '');

/** Job statuses, as the Jobs page shows them. */
export const JOB_STATUSES: Record<string, OutcomeView> = {
  QUEUED: { label: 'Queued', type: 'pending' },
  RUNNING: { label: 'Running', type: 'in-progress' },
  SUCCEEDED: { label: 'Succeeded', type: 'success' },
  FAILED: { label: 'Failed', type: 'error' },
};

export const jobStatusView = (status: string): OutcomeView => JOB_STATUSES[status] ?? { label: status, type: 'info' };

export const JOB_TYPE_LABELS: Record<string, string> = { SKIP_ALL_REVIEWS: 'Skip All Reviews' };
export const JOB_MODE_LABELS: Record<string, string> = { SELECTED: 'Selected documents', PROFILE: 'By profile' };
