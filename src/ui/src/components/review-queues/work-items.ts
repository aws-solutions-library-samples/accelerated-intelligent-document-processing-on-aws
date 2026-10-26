// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// One row shape for every kind of HITL work: production review queue items and Test
// Studio documents. Queue Management and Home › Tasks list both; each row opens
// the edit screen for its item. An Admin assigns work, or an Annotator listed
// on the queue claims it.
import type { ReviewItem, TestSetTask } from '../../graphql/generated/operation-types';
import { reviewItemHref, testSetAnnotateHref } from '../../routes/constants';
import { QueueItemState, reviewItemStateView } from '../common/review-status';
import type { StateView } from '../common/review-status';
import { documentName, reasonText } from './review-api';

export type WorkSource = 'production' | 'test-set';

export interface WorkItem {
  /** Unique across sources. */
  id: string;
  source: WorkSource;
  objectKey: string;
  name: string;
  /** Production: the queue id. Test set: the test set name. */
  queue: string;
  priority: number | null;
  /** Production: when the item entered its queue. Test set: when it was assigned. */
  enqueuedAt: string | null;
  reason: string | null;
  extraReasons: number;
  state: StateView;
  /** The Annotator the item is assigned to ("You" for the caller), or null. */
  assignedTo: string | null;
  assignedToMe: boolean;
  href: string;
  /** The production item, when there is one (Admin actions such as Assign need it). */
  reviewItem?: ReviewItem;
  /** Test-set rows: the set, and the review copy completeSectionReview saves to. */
  testSetId?: string;
  reviewObjectKey?: string | null;
}

export const SOURCE_LABELS: Record<WorkSource, string> = {
  production: 'Production',
  'test-set': 'Test Studio',
};

const ASSIGNED: StateView = { label: 'Assigned', type: 'in-progress' };

export const fromReviewItem = (item: ReviewItem): WorkItem => ({
  id: `p:${item.objectKey}`,
  source: 'production',
  objectKey: item.objectKey,
  name: documentName(item.objectKey),
  queue: `${item.queue}${item.escalated ? ' (escalated)' : ''}`,
  priority: item.priority,
  enqueuedAt: item.enqueuedAt,
  reason: item.reasons[0] ? reasonText(item.reasons[0]) : null,
  extraReasons: Math.max(item.reasonCount - 1, 0),
  // The backend state for an item held by its Annotator is CLAIMED; the UI says Assigned.
  state: item.state === QueueItemState.CLAIMED ? ASSIGNED : reviewItemStateView(item.state),
  assignedTo: item.isMine ? 'You' : (item.ownerEmail ?? null),
  assignedToMe: item.isMine && item.state === QueueItemState.CLAIMED,
  href: reviewItemHref(item.objectKey),
  reviewItem: item,
});

/** One of the caller's assigned Test Studio documents (listMyTestSetTasks). */
export const fromTestSetTask = (task: TestSetTask): WorkItem => ({
  id: `t:${task.testSetId}:${task.objectKey}`,
  source: 'test-set',
  objectKey: task.objectKey,
  name: documentName(task.objectKey),
  queue: task.testSetId,
  priority: null,
  enqueuedAt: task.assignedAt ?? null,
  reason: task.alertCount
    ? `${task.alertCount} confidence alert${task.alertCount === 1 ? '' : 's'}`
    : task.labelSource
      ? 'Draft labels to confirm'
      : null,
  extraReasons: 0,
  state: ASSIGNED,
  assignedTo: 'You',
  assignedToMe: true,
  href: testSetAnnotateHref(task.testSetId, task.objectKey),
  testSetId: task.testSetId,
});
