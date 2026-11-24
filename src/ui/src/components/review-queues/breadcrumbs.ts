// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The breadcrumbs of the Review Studio pages, an Annotator's Queues and Tasks, and the
// edit page. Review Management sits under Review Studio, which has no page of its own,
// so the Review Studio crumb opens Review Management. The edit page's trail follows
// where the user opened it from (`?from=`, see EditOrigin); without one it is the
// role's usual trail.
import type { BreadcrumbGroupProps } from '@cloudscape-design/components';
import { defineMessages, type IntlShape, type NoMessageValues, type TypedMessageDescriptor } from 'react-intl';

import {
  DOCUMENTS_PATH,
  INBOX_PATH,
  JOBS_PATH,
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

const messages = defineMessages<{
  home: NoMessageValues;
  tasks: NoMessageValues;
  queues: NoMessageValues;
  jobs: NoMessageValues;
  inbox: NoMessageValues;
  testStudio: NoMessageValues;
  reviewStudio: NoMessageValues;
  reviewManagement: NoMessageValues;
  edit: NoMessageValues;
}>({
  home: {
    id: 'breadcrumbs.reviewStudio.home',
    defaultMessage: 'Home',
    description: 'First crumb of the breadcrumb trail on Home › Tasks, Home › Queues, Home › Jobs and the pages under them.',
  },
  tasks: {
    id: 'breadcrumbs.reviewStudio.tasks',
    defaultMessage: 'Tasks',
    description: 'Crumb of Home › Tasks, the review items assigned to the reader, in the breadcrumb trail.',
  },
  queues: {
    id: 'breadcrumbs.reviewStudio.queues',
    defaultMessage: 'Queues',
    description: 'Crumb of Home › Queues, the review queues the reader works, in the breadcrumb trail.',
  },
  jobs: {
    id: 'breadcrumbs.reviewStudio.jobs',
    defaultMessage: 'Jobs',
    description: 'Crumb of Home › Jobs, the bulk Skip All Reviews jobs, in the breadcrumb trail.',
  },
  inbox: {
    id: 'breadcrumbs.reviewStudio.inbox',
    defaultMessage: 'Inbox',
    description: "Crumb of Home › Inbox in the breadcrumb trail of an item's edit page opened from an Inbox message.",
  },
  testStudio: {
    id: 'breadcrumbs.reviewStudio.testStudio',
    defaultMessage: 'Test Studio',
    description: "First crumb of the breadcrumb trail of a Test Studio document's edit page. Test Studio is the name of the screen.",
  },
  reviewStudio: {
    id: 'breadcrumbs.reviewStudio.reviewStudio',
    defaultMessage: 'Review Studio',
    description: 'First crumb of the breadcrumb trail on Review Management and the pages under it; it opens Review Management.',
  },
  reviewManagement: {
    id: 'breadcrumbs.reviewStudio.reviewManagement',
    defaultMessage: 'Review Management',
    description: 'Crumb of Review Management, where an Admin manages the review queues, in the breadcrumb trail.',
  },
  edit: {
    id: 'breadcrumbs.reviewStudio.edit',
    defaultMessage: 'Edit',
    description: "Last crumb of the breadcrumb trail of a document's edit page opened from the document page.",
  },
});

/** The label in the UI's language; without `intl`, the English source text. */
const labelOf = (message: TypedMessageDescriptor<NoMessageValues>, intl?: IntlShape): string =>
  intl ? intl.formatMessage(message) : String(message.defaultMessage);

/** Test Studio on its test sets tab, where its crumb and its navigation entry lead. */
const TEST_SETS_HREF = `#${TEST_STUDIO_PATH}?tab=sets`;

export const homeCrumb = (intl?: IntlShape): Crumb => ({ text: labelOf(messages.home, intl), href: '#/' });
export const tasksCrumb = (intl?: IntlShape): Crumb => ({ text: labelOf(messages.tasks, intl), href: `#${MY_TASKS_PATH}` });
export const myQueuesCrumb = (intl?: IntlShape): Crumb => ({ text: labelOf(messages.queues, intl), href: `#${MY_QUEUES_PATH}` });
export const jobsCrumb = (intl?: IntlShape): Crumb => ({ text: labelOf(messages.jobs, intl), href: `#${JOBS_PATH}` });
const inboxCrumb = (intl?: IntlShape): Crumb => ({ text: labelOf(messages.inbox, intl), href: inboxHref() });
const testStudioCrumb = (intl?: IntlShape): Crumb => ({ text: labelOf(messages.testStudio, intl), href: TEST_SETS_HREF });

/** Review Studio › Review Management, on the table the trail came from. */
export const reviewManagementTrail = (view?: ReviewManagementView, intl?: IntlShape): Crumb[] => [
  { text: labelOf(messages.reviewStudio, intl), href: reviewManagementHref(view) },
  { text: labelOf(messages.reviewManagement, intl), href: reviewManagementHref(view) },
];

/** A queue's page: under Review Management for an Admin, under Home › Queues for an Annotator. */
export const queuePageTrail = (isAdmin: boolean, queue: Crumb, view: ReviewManagementView, intl?: IntlShape): Crumb[] =>
  isAdmin ? [...reviewManagementTrail(view, intl), queue] : [homeCrumb(intl), myQueuesCrumb(intl), queue];

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
export const editTrail = ({ origin, objectKey, name, queue, testSet }: EditTrailInput, intl?: IntlShape): Crumb[] => {
  const doc: Crumb = { text: name, href: '#' };
  switch (origin) {
    case 'document':
      return [
        ...documentListBreadcrumbItems(intl),
        { text: name, href: documentDetailHref(objectKey) },
        { text: labelOf(messages.edit, intl), href: '#' },
      ];
    case 'review':
      return [...reviewManagementTrail(queue?.view, intl), ...(queue ? [queue.crumb] : []), doc];
    case 'queues':
      return [homeCrumb(intl), myQueuesCrumb(intl), ...(queue ? [queue.crumb] : []), doc];
    case 'tasks':
      return [homeCrumb(intl), tasksCrumb(intl), doc];
    case 'inbox':
      return [homeCrumb(intl), inboxCrumb(intl), doc];
    case 'test-studio':
      return [testStudioCrumb(intl), ...(testSet ? [testSet] : []), doc];
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
      return TEST_SETS_HREF;
  }
};
