// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The UI's one declaration of review and legacy HITL status values, mirroring
// lib/idp_common_pkg/idp_common/review_queues/statuses.py.
import type { StatusIndicatorProps } from '@cloudscape-design/components';
import { defineMessages, type IntlShape, type NoMessageValues, type TypedMessageDescriptor } from 'react-intl';

export const ReviewStatus = {
  IN_REVIEW: 'in_review',
  COMPLETE: 'complete',
  REJECTED: 'rejected',
} as const;
export type ReviewStatusValue = (typeof ReviewStatus)[keyof typeof ReviewStatus];

export const QueueItemState = {
  HOLDING: 'HOLDING',
  OPEN: 'OPEN',
  CLAIMED: 'CLAIMED',
  FINALIZING: 'FINALIZING',
  COMPLETE: 'COMPLETE',
  REJECTED: 'REJECTED',
  FINALIZE_FAILED: 'FINALIZE_FAILED',
  WITHDRAWN: 'WITHDRAWN',
} as const;
export type QueueItemStateValue = (typeof QueueItemState)[keyof typeof QueueItemState];

export const LegacyHitlStatus = {
  PENDING_REVIEW: 'PendingReview',
  REVIEW_PENDING: 'Review Pending',
  IN_PROGRESS: 'InProgress',
  REVIEW_IN_PROGRESS: 'ReviewInProgress',
  COMPLETED: 'Completed',
  REVIEW_COMPLETED: 'Review Completed',
  SKIPPED: 'Skipped',
  REVIEW_SKIPPED: 'Review Skipped',
} as const;

export const LEGACY_PENDING: ReadonlySet<string> = new Set([
  LegacyHitlStatus.PENDING_REVIEW,
  LegacyHitlStatus.REVIEW_PENDING,
  LegacyHitlStatus.IN_PROGRESS,
]);
export const LEGACY_DONE: ReadonlySet<string> = new Set([
  LegacyHitlStatus.COMPLETED,
  LegacyHitlStatus.REVIEW_COMPLETED,
  LegacyHitlStatus.SKIPPED,
  LegacyHitlStatus.REVIEW_SKIPPED,
]);
export const LIVE_ITEM_STATES: ReadonlySet<string> = new Set([
  QueueItemState.HOLDING,
  QueueItemState.OPEN,
  QueueItemState.CLAIMED,
  QueueItemState.FINALIZING,
]);
export const TERMINAL_ITEM_STATES: ReadonlySet<string> = new Set([
  QueueItemState.COMPLETE,
  QueueItemState.REJECTED,
  QueueItemState.FINALIZE_FAILED,
  QueueItemState.WITHDRAWN,
]);

export type HitlKind = 'none' | 'pending' | 'in_progress' | 'in_review' | 'complete' | 'rejected' | 'skipped';
export interface HitlView {
  kind: HitlKind;
  queueManaged: boolean;
}

const LEGACY_KINDS: Record<string, HitlKind> = {
  [LegacyHitlStatus.PENDING_REVIEW]: 'pending',
  [LegacyHitlStatus.REVIEW_PENDING]: 'pending',
  [LegacyHitlStatus.IN_PROGRESS]: 'in_progress',
  [LegacyHitlStatus.REVIEW_IN_PROGRESS]: 'in_progress',
  [LegacyHitlStatus.COMPLETED]: 'complete',
  [LegacyHitlStatus.REVIEW_COMPLETED]: 'complete',
  [LegacyHitlStatus.SKIPPED]: 'skipped',
  [LegacyHitlStatus.REVIEW_SKIPPED]: 'skipped',
};

const REVIEW_KINDS: Record<string, HitlKind> = {
  [ReviewStatus.IN_REVIEW]: 'in_review',
  [ReviewStatus.COMPLETE]: 'complete',
  [ReviewStatus.REJECTED]: 'rejected',
};

/** Normalise a stored HITLStatus value (exact match after trim, like the Python reader). */
export const normalizeHitlStatus = (raw: string | null | undefined): HitlView => {
  if (raw == null) return { kind: 'none', queueManaged: false };
  const value = String(raw).trim();
  if (value in REVIEW_KINDS) return { kind: REVIEW_KINDS[value], queueManaged: true };
  if (value in LEGACY_KINDS) return { kind: LEGACY_KINDS[value], queueManaged: false };
  return { kind: 'none', queueManaged: false };
};

export const isQueueManaged = (raw: string | null | undefined): boolean => normalizeHitlStatus(raw).queueManaged;

/**
 * Whether the document's review has finished: a legacy review completed or skipped, or
 * a queue review that ended complete or rejected.
 *
 * `COMPLETED` is the one finished spelling a real writer stored that the exact-match
 * normaliser does not know: the earlier SageMaker A2I flow wrote it, and retained rows
 * may still carry it. The server-side check in `process_changes_resolver` makes the
 * same exception.
 */
export const isHitlReviewFinished = (raw: string | null | undefined): boolean => {
  const { kind } = normalizeHitlStatus(raw);
  return kind === 'complete' || kind === 'rejected' || kind === 'skipped' || raw === 'COMPLETED';
};

/**
 * A document waiting in a production review queue. It is reviewed from its queue item
 * (Edit, on the document page), and edited nowhere else until the review finishes: an edit made on the
 * document page bypasses the item's assignment and is overwritten by the reviewer's
 * corrections when the review completes.
 */
export const isInReviewQueue = (raw: string | null | undefined): boolean => {
  const view = normalizeHitlStatus(raw);
  return view.queueManaged && view.kind === 'in_review';
};

/**
 * Why a legacy review is finished without anyone checking it (Skip All Reviews).
 * The server accepts only these: `SKIP_ALL_REASONS` in
 * `lib/idp_common_pkg/idp_common/review_queues/statuses.py`, asserted equal by
 * `review-status.skipAllReasons.test.ts`.
 */
export const SKIP_ALL_REASONS: readonly string[] = [
  'Extracted values look correct',
  'Review not needed for this document',
  'Duplicate document',
  'Other (see note)',
];

/**
 * Why a finished production document is sent to a review queue by hand (Send to
 * review). The server accepts only these: `SEND_TO_REVIEW_REASONS` in
 * `lib/idp_common_pkg/idp_common/review_queues/statuses.py`, asserted equal by
 * `review-status.skipAllReasons.test.ts`.
 */
export const SEND_TO_REVIEW_REASONS: readonly string[] = [
  'Spot check',
  'Suspected extraction error',
  'Customer or downstream query',
  'Other (see note)',
];

/** The reason (and note) recorded with a document's latest Skip All Reviews, if any. */
export const lastSkipAllReason = (history: unknown): { reason: string; note?: string } | null => {
  const entries = (Array.isArray(history) ? history : []) as Array<Record<string, unknown> | null>;
  const entry = [...entries].reverse().find((e) => e?.action === 'skip_all' && typeof e.reason === 'string');
  if (!entry) return null;
  return { reason: entry.reason as string, note: typeof entry.note === 'string' ? entry.note : undefined };
};

/** Why editing is off on the document page while it is in a review queue. */
export const inReviewQueueEditReason = (canWorkReviewQueues: boolean): string =>
  canWorkReviewQueues
    ? 'This document is in a review queue. Choose Edit to work on it there.'
    : 'This document is in a review queue. An Admin, Annotator or Reviewer reviews it there.';

export interface StateView {
  label: string;
  type: StatusIndicatorProps.Type;
}

/**
 * One label per queue item state the backend writes (`QueueItemState` in
 * idp_common/review_queues/statuses.py); scripts/tests/test_review_status_ui_parity.py
 * checks the two lists match.
 */
const stateMessages = defineMessages<Record<QueueItemStateValue, NoMessageValues>>({
  HOLDING: {
    id: 'common.reviewStatus.state.holding',
    defaultMessage: 'Preparing',
    description:
      "State of a review queue item that is being prepared and is not yet waiting for review. Shown in a queue's Documents table and on the item's edit page.",
  },
  OPEN: {
    id: 'common.reviewStatus.state.open',
    defaultMessage: 'Waiting',
    description:
      "State of a review queue item waiting in its queue for an Annotator to claim it or an Admin to assign it. Shown in a queue's Documents table and on the item's edit page.",
  },
  CLAIMED: {
    id: 'common.reviewStatus.state.claimed',
    defaultMessage: 'Claimed',
    description:
      "State of a review queue item an Annotator holds. Shown on the item's edit page; the work tables say Assigned for it instead.",
  },
  FINALIZING: {
    id: 'common.reviewStatus.state.finalizing',
    defaultMessage: 'Finalizing',
    description:
      "State of a review queue item whose review is done and whose result is being applied to the document. Shown in a queue's Documents table and on the item's edit page.",
  },
  COMPLETE: {
    id: 'common.reviewStatus.state.complete',
    defaultMessage: 'Complete',
    description: "State of a review queue item whose review is done. Shown in a queue's Documents table and on the item's edit page.",
  },
  REJECTED: {
    id: 'common.reviewStatus.state.rejected',
    defaultMessage: 'Rejected',
    description:
      "State of a review queue item whose document a reviewer rejected. Shown in a queue's Documents table and on the item's edit page.",
  },
  FINALIZE_FAILED: {
    id: 'common.reviewStatus.state.finalizeFailed',
    defaultMessage: 'Finalize failed',
    description:
      "State of a review queue item whose review is done but whose result could not be applied to the document yet; it is retried. Shown in a queue's Documents table and on the item's edit page.",
  },
  WITHDRAWN: {
    id: 'common.reviewStatus.state.withdrawn',
    defaultMessage: 'Withdrawn',
    description:
      "State of a review queue item taken out of review without a result, for example because its document was processed again. Shown in a queue's Documents table and on the item's edit page.",
  },
});

const messages = defineMessages<{ unknownState: NoMessageValues }>({
  unknownState: {
    id: 'common.reviewStatus.state.unknown',
    defaultMessage: 'Unknown',
    description: 'State label of a review queue item when no state is known for it.',
  },
});

/** The StatusIndicator type of each queue item state (Cloudscape's names, not text). */
const STATE_TYPES: Record<QueueItemStateValue, StatusIndicatorProps.Type> = {
  [QueueItemState.HOLDING]: 'pending',
  [QueueItemState.OPEN]: 'pending',
  [QueueItemState.CLAIMED]: 'in-progress',
  [QueueItemState.FINALIZING]: 'loading',
  [QueueItemState.COMPLETE]: 'success',
  [QueueItemState.REJECTED]: 'stopped',
  [QueueItemState.FINALIZE_FAILED]: 'error',
  [QueueItemState.WITHDRAWN]: 'stopped',
};

const isQueueItemState = (state: string): state is QueueItemStateValue => Object.hasOwn(stateMessages, state);

const labelOf = (message: TypedMessageDescriptor<NoMessageValues>, intl?: IntlShape): string =>
  intl ? intl.formatMessage(message) : String(message.defaultMessage);

/**
 * A queue item state's label and StatusIndicator type. The label is in the UI's language with
 * `intl`; without it, the English source text. A state the UI does not know is shown as stored.
 */
export const reviewItemStateView = (state: string | null | undefined, intl?: IntlShape): StateView =>
  state != null && isQueueItemState(state)
    ? { label: labelOf(stateMessages[state], intl), type: STATE_TYPES[state] }
    : { label: state ?? labelOf(messages.unknownState, intl), type: 'info' };
