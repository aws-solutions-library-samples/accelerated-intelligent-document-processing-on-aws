// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// One row shape for every kind of HITL work: production review queue items and Test
// Studio documents. Review Management and Home › Tasks list both; each row opens
// the edit screen for its item. An Admin assigns work, or an Annotator listed
// on the queue claims it.
//
// Rows are built while data loads (a queue page's load, the ReviewWork provider's
// poll), so they hold data only: codes, flags and the reason as given. The table
// words them where it shows them, through `workItemView`.
import { defineMessages, type IntlShape, type NoMessageValues } from 'react-intl';

import type { ReviewItem, ReviewReason, TestSetTask } from '../../graphql/generated/operation-types';
import { documentEditHref, testSetAnnotateHref } from '../../routes/constants';
import type { EditOrigin } from '../../routes/constants';
import { QueueItemState, reviewItemStateView } from '../common/review-status';
import { documentName, reasonText } from './review-api';
import { productionWorkItemId, testSetWorkItemId } from './work-item-ids';

export type WorkSource = 'production' | 'test-set';

/** Why a row needs work, as data: a review reason, a Test Studio document's alerts, or its draft labels. */
export type WorkItemReason = { kind: 'review'; reason: ReviewReason } | { kind: 'alerts'; count: number } | { kind: 'draftLabels' };

/** The state a work list shows for an item its Annotator holds (CLAIMED) and for every Test Studio task. */
export const ASSIGNED_STATE = 'ASSIGNED';

export interface WorkItem {
  /** Unique across sources. */
  id: string;
  source: WorkSource;
  objectKey: string;
  name: string;
  /** Production: the queue id. Test set: the test set name. */
  queue: string;
  /** The item is escalated (production only). */
  escalated: boolean;
  priority: number | null;
  /** Production: when the item entered its queue. Test set: when it was assigned. */
  enqueuedAt: string | null;
  /** The top reason, as data. */
  reason: WorkItemReason | null;
  extraReasons: number;
  /** The queue item state (`QueueItemState`), or `ASSIGNED_STATE`. */
  state: string;
  /** The e-mail of the Annotator the item is assigned to, or null. */
  assignedTo: string | null;
  /** The item is the caller's, so it shows as "You" (every Test Studio task is). */
  assignedToYou: boolean;
  assignedToMe: boolean;
  href: string;
  /** The production item, when there is one (Admin actions such as Assign need it). */
  reviewItem?: ReviewItem;
  /** Test-set rows: the set, and the review copy completeSectionReview saves to. */
  testSetId?: string;
  reviewObjectKey?: string | null;
}

const messages = defineMessages<{
  sourceProduction: NoMessageValues;
  sourceTestSet: NoMessageValues;
  assigned: NoMessageValues;
  escalated: { readonly queue: string };
  you: NoMessageValues;
  alerts: { readonly count: number };
  draftLabels: NoMessageValues;
}>({
  sourceProduction: {
    id: 'reviewQueues.workItem.source.production',
    defaultMessage: 'Production',
    description:
      'Source of a review work item or queue: a production review queue (as opposed to Test Studio). Shown as a badge in the Source column of Home › Tasks, Home › Queues and a queue page, and in the source filter.',
  },
  sourceTestSet: {
    id: 'reviewQueues.workItem.source.testSet',
    defaultMessage: 'Test Studio',
    description:
      'Source of a review work item or queue: a Test Studio test set being annotated. Shown as a badge in the Source column of Home › Tasks, Home › Queues and a queue page, and in the source filter. Test Studio is the name of the screen.',
  },
  assigned: {
    id: 'reviewQueues.workItem.state.assigned',
    defaultMessage: 'Assigned',
    description:
      'State column of a review work table for an item an Annotator holds, and for every Test Studio document assigned to the reader.',
  },
  escalated: {
    id: 'reviewQueues.workItem.escalated',
    defaultMessage: '{queue} (escalated)',
    description: 'Queue column of a review work table for an escalated item. {queue} is the queue id, as an Admin named it.',
  },
  you: {
    id: 'reviewQueues.workItem.you',
    defaultMessage: 'You',
    description:
      'Annotator column of a review work table, and the end of a queue page\'s "Assigned to …" hint, for an item assigned to the reader.',
  },
  alerts: {
    id: 'reviewQueues.workItem.alerts',
    defaultMessage: '{count, plural, one {# confidence alert} other {# confidence alerts}}',
    description:
      'Top reason column of Home › Tasks for a Test Studio document. {count} is the number of its fields below their confidence threshold.',
  },
  draftLabels: {
    id: 'reviewQueues.workItem.draftLabels',
    defaultMessage: 'Draft labels to confirm',
    description:
      'Top reason column of Home › Tasks for a Test Studio document that has no confidence alert but has labels for the Annotator to check and confirm.',
  },
});

/** A work source's label (the Source badge and filter option), in the UI's language. */
export const workSourceLabel = (source: WorkSource, intl: IntlShape): string =>
  intl.formatMessage(source === 'production' ? messages.sourceProduction : messages.sourceTestSet);

/** A work row as a table shows it: each label in the UI's language. */
export interface WorkItemView {
  /** The queue id, with "(escalated)" when the item is escalated. */
  queue: string;
  state: string;
  /** "You" for the caller's item, else the Annotator's e-mail; null when no one is assigned. */
  assignee: string | null;
  reason: string | null;
}

const reasonLabel = (reason: WorkItemReason | null, intl: IntlShape): string | null => {
  if (!reason) return null;
  switch (reason.kind) {
    case 'review':
      return reasonText(reason.reason, intl);
    case 'alerts':
      return intl.formatMessage(messages.alerts, { count: reason.count });
    case 'draftLabels':
      return intl.formatMessage(messages.draftLabels);
  }
};

/** Words a row for its table: the cells, the text and assignment filters and the sorting all read this. */
export const workItemView = (item: WorkItem, intl: IntlShape): WorkItemView => ({
  queue: item.escalated ? intl.formatMessage(messages.escalated, { queue: item.queue }) : item.queue,
  state: item.state === ASSIGNED_STATE ? intl.formatMessage(messages.assigned) : reviewItemStateView(item.state, intl).label,
  assignee: item.assignedToYou ? intl.formatMessage(messages.you) : item.assignedTo,
  reason: reasonLabel(item.reason, intl),
});

/** One production review item, as a row whose link opens the edit page with its origin. */
export const fromReviewItem =
  (from: EditOrigin) =>
  (item: ReviewItem): WorkItem => ({
    id: productionWorkItemId(item.objectKey),
    source: 'production',
    objectKey: item.objectKey,
    name: documentName(item.objectKey),
    queue: item.queue,
    escalated: item.escalated,
    priority: item.priority,
    enqueuedAt: item.enqueuedAt,
    reason: item.reasons[0] ? { kind: 'review', reason: item.reasons[0] } : null,
    extraReasons: Math.max(item.reasonCount - 1, 0),
    // The backend state for an item held by its Annotator is CLAIMED; the UI says Assigned.
    state: item.state === QueueItemState.CLAIMED ? ASSIGNED_STATE : item.state,
    assignedTo: item.ownerEmail ?? null,
    assignedToYou: item.isMine,
    assignedToMe: item.isMine && item.state === QueueItemState.CLAIMED,
    href: documentEditHref(item.objectKey, { from }),
    reviewItem: item,
  });

/** One of the caller's assigned Test Studio documents (listMyTestSetTasks). */
export const fromTestSetTask = (task: TestSetTask): WorkItem => ({
  id: testSetWorkItemId(task.testSetId, task.objectKey),
  source: 'test-set',
  objectKey: task.objectKey,
  name: documentName(task.objectKey),
  queue: task.testSetId,
  escalated: false,
  priority: null,
  enqueuedAt: task.assignedAt ?? null,
  reason: task.alertCount ? { kind: 'alerts', count: task.alertCount } : task.labelSource ? { kind: 'draftLabels' } : null,
  extraReasons: 0,
  state: ASSIGNED_STATE,
  assignedTo: task.assignedTo ?? null,
  assignedToYou: true,
  assignedToMe: true,
  href: testSetAnnotateHref(task.testSetId, task.objectKey, 'tasks'),
  testSetId: task.testSetId,
});
