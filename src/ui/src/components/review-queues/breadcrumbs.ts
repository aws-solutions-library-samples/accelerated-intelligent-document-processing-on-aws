// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The breadcrumbs of the Review Studio pages, an Annotator's Queues and Tasks, and the
// edit page. Review Management sits under Review Studio, which has no page of its own,
// so the Review Studio crumb opens Review Management. The edit page's trail follows
// where the user opened it from (`?from=`, see EditOrigin); without one it is the
// role's usual trail.
import type { BreadcrumbGroupProps } from '@cloudscape-design/components';

import {
  DOCUMENTS_PATH,
  INBOX_PATH,
  MY_QUEUES_PATH,
  MY_TASKS_PATH,
  REVIEW_QUEUES_PATH,
  TEST_STUDIO_PATH,
  documentDetailHref,
  inboxHref,
  reviewManagementHref,
} from '../../routes/constants';
import type { EditOrigin, ReviewManagementView } from '../../routes/constants';
import { documentListBreadcrumbItems } from '../document-list/breadcrumbs';

type Crumb = BreadcrumbGroupProps.Item;

export const HOME_CRUMB: Crumb = { text: 'Home', href: '#/' };
export const TASKS_CRUMB: Crumb = { text: 'Tasks', href: `#${MY_TASKS_PATH}` };
export const MY_QUEUES_CRUMB: Crumb = { text: 'Queues', href: `#${MY_QUEUES_PATH}` };
const INBOX_CRUMB: Crumb = { text: 'Inbox', href: inboxHref() };
const TEST_STUDIO_CRUMB: Crumb = { text: 'Test Studio', href: `#${TEST_STUDIO_PATH}?tab=sets` };

/** Review Studio › Review Management, on the table the trail came from. */
export const reviewManagementTrail = (view?: ReviewManagementView): Crumb[] => [
  { text: 'Review Studio', href: reviewManagementHref(view) },
  { text: 'Review Management', href: reviewManagementHref(view) },
];

/** A queue's page: under Review Management for an Admin, under Home › Queues for an Annotator. */
export const queuePageTrail = (isAdmin: boolean, queue: Crumb, view: ReviewManagementView): Crumb[] =>
  isAdmin ? [...reviewManagementTrail(view), queue] : [HOME_CRUMB, MY_QUEUES_CRUMB, queue];

export interface EditTrailInput {
  origin: EditOrigin;
  objectKey: string;
  /** documentName(objectKey). */
  name: string;
  /** The item's queue, for a trail through Review Management or Queues; null until it is known. */
  queue?: { crumb: Crumb; view: ReviewManagementView } | null;
  /** A Test Studio item's set (its name and its page in Test Studio). */
  testSet?: Crumb | null;
}

/** The edit page's breadcrumbs, by where it was opened from; the document is last. */
export const editTrail = ({ origin, objectKey, name, queue, testSet }: EditTrailInput): Crumb[] => {
  const doc: Crumb = { text: name, href: '#' };
  switch (origin) {
    case 'document':
      return [...documentListBreadcrumbItems, { text: name, href: documentDetailHref(objectKey) }, { text: 'Edit', href: '#' }];
    case 'review':
      return [...reviewManagementTrail(queue?.view), ...(queue ? [queue.crumb] : []), doc];
    case 'queues':
      return [HOME_CRUMB, MY_QUEUES_CRUMB, ...(queue ? [queue.crumb] : []), doc];
    case 'tasks':
      return [HOME_CRUMB, TASKS_CRUMB, doc];
    case 'inbox':
      return [HOME_CRUMB, INBOX_CRUMB, doc];
    case 'test-studio':
      return [TEST_STUDIO_CRUMB, ...(testSet ? [testSet] : []), doc];
  }
};

/** The side-navigation entry the edit page highlights, by where it was opened from. */
export const editActiveHref = (origin: EditOrigin): string => {
  switch (origin) {
    case 'document':
      return `#${DOCUMENTS_PATH}`;
    case 'review':
      return `#${REVIEW_QUEUES_PATH}`;
    case 'queues':
      return `#${MY_QUEUES_PATH}`;
    case 'tasks':
      return `#${MY_TASKS_PATH}`;
    case 'inbox':
      return `#${INBOX_PATH}`;
    case 'test-studio':
      return `#${TEST_STUDIO_PATH}?tab=sets`;
  }
};
