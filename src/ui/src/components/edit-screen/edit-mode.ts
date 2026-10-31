// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Which mode a document's edit page opens in (/documents/:key/edit): its review-queue
// item while it is in review and the caller can work queues (Admin, Annotator),
// otherwise the direct edit. The item's state decides when there is an item; the
// tracking row's HITLStatus is only its mirror. The choice fails closed: a document in
// review never opens as an editable direct edit — an unreadable item is an error with
// Retry, and a lagging mirror opens the direct edit read-only.
import type { ReviewItemDetail } from '../../graphql/generated/operation-types';
import { isAuthorizationError } from '../../hooks/utils/graphql-error';
import { QueueItemState, normalizeHitlStatus } from '../common/review-status';
import { reviewErrorCode } from '../review-queues/review-api';
import type { ReviewActivity } from './edit-model';

/**
 * Item states in which the document is in review: the queue owns it. Mirrors
 * `_STATE_TO_STATUS` in `idp_common/review_queues/statuses.py` (every state that maps
 * to IN_REVIEW). Not `TERMINAL_ITEM_STATES`, which counts FINALIZE_FAILED as over.
 */
export const IN_REVIEW_ITEM_STATES: ReadonlySet<string> = new Set([
  QueueItemState.HOLDING,
  QueueItemState.OPEN,
  QueueItemState.CLAIMED,
  QueueItemState.FINALIZING,
  QueueItemState.FINALIZE_FAILED,
]);

/** Why a direct edit of a document in review is read-only. */
export type DirectLock = 'in-review-elsewhere' | 'mirror-lag';

export type EditModeDecision =
  | { mode: 'direct'; lock?: DirectLock; reviewActivity?: ReviewActivity }
  | { mode: 'queue'; detail: ReviewItemDetail }
  | { mode: 'not-a-member' }
  | { mode: 'item-failed'; error: unknown };

export type ItemRead = { kind: 'not-read' } | { kind: 'read'; detail: ReviewItemDetail } | { kind: 'failed'; error: unknown };

/** The review item is read only for a queue-managed document and a caller who can work queues. */
export const needsItemRead = (hitlStatus: string | null | undefined, canWorkReviewQueues: boolean): boolean =>
  canWorkReviewQueues && normalizeHitlStatus(hitlStatus).queueManaged;

const MEMBERSHIP_CODES = new Set(['NOT_A_MEMBER', 'OUT_OF_SCOPE', 'NOT_ASSIGNED']);

const errorTypes = (err: unknown): string[] => {
  if (!err || typeof err !== 'object') return [];
  const envelope = err as { errorType?: string; errors?: { errorType?: string }[] };
  return [envelope.errorType, ...(envelope.errors ?? []).map((e) => e?.errorType)].filter((t): t is string => !!t);
};

const isNotFound = (err: unknown): boolean =>
  reviewErrorCode(err) === 'ITEM_NOT_FOUND' || errorTypes(err).some((t) => t === 'ResourceNotFound' || t === 'NotFound');

export const decideEditMode = ({
  hitlStatus,
  canWorkReviewQueues,
  annotatorOnly,
  item,
}: {
  hitlStatus?: string | null;
  canWorkReviewQueues: boolean;
  annotatorOnly: boolean;
  item: ItemRead;
}): EditModeDecision => {
  const view = normalizeHitlStatus(hitlStatus);
  // Not queue-managed (no review, a legacy status, or a withdrawn item's cleared row).
  if (!view.queueManaged) return { mode: 'direct' };
  // A finished queue review: direct, with its review activity when the item was read.
  if (view.kind !== 'in_review') return item.kind === 'read' ? { mode: 'direct', reviewActivity: item.detail.history } : { mode: 'direct' };
  // In review, for a role that does not work queues: read-only, edited in the queue.
  if (!canWorkReviewQueues) return { mode: 'direct', lock: 'in-review-elsewhere' };
  if (item.kind === 'not-read') return { mode: 'item-failed', error: new Error('The review item was not read') };
  if (item.kind === 'failed') {
    const { error } = item;
    if (isNotFound(error)) return { mode: 'direct', lock: 'mirror-lag' };
    const code = reviewErrorCode(error);
    if ((code && MEMBERSHIP_CODES.has(code)) || isAuthorizationError(error)) {
      return annotatorOnly ? { mode: 'not-a-member' } : { mode: 'direct', lock: 'in-review-elsewhere' };
    }
    return { mode: 'item-failed', error };
  }
  if (IN_REVIEW_ITEM_STATES.has(item.detail.item.state)) return { mode: 'queue', detail: item.detail };
  // The mirror says in review, but the item has finished (or is a state this UI doesn't know).
  return { mode: 'direct', lock: 'mirror-lag' };
};
