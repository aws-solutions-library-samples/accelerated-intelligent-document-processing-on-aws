// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import React from 'react';
import { Badge } from '@cloudscape-design/components';
import type { SideNavigationProps } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, type IntlShape, type NoMessageValues, type TypedMessageDescriptor } from 'react-intl';

import { JOBS_PATH, MY_QUEUES_PATH, MY_TASKS_PATH, REVIEW_QUEUES_PATH } from '../../routes/constants';

const messages = defineMessages<{
  home: NoMessageValues;
  tasks: NoMessageValues;
  tasksAssigned: { readonly count: number };
  queues: NoMessageValues;
  jobs: NoMessageValues;
  reviewStudio: NoMessageValues;
  management: NoMessageValues;
}>({
  home: {
    id: 'navigation.home.section',
    defaultMessage: 'Home',
    description: 'First section of the side navigation, for every user: the Inbox, and for an Annotator the Tasks and Queues.',
  },
  tasks: {
    id: 'navigation.home.tasks',
    defaultMessage: 'Tasks',
    description: 'Side-navigation link under Home to the review items assigned to the user (Annotators).',
  },
  tasksAssigned: {
    id: 'navigation.home.tasksAssigned',
    defaultMessage: '{count, plural, one {assigned} other {assigned}}',
    description:
      'Read by screen readers after the count badge of the Tasks link, as in "3 assigned". {count} is the number of review items assigned to the user; the badge itself shows the number.',
  },
  queues: {
    id: 'navigation.home.queues',
    defaultMessage: 'Queues',
    description: 'Side-navigation link under Home to the review queues that list the user as an Annotator.',
  },
  jobs: {
    id: 'navigation.home.jobs',
    defaultMessage: 'Jobs',
    description: 'Side-navigation link under Home to the list of bulk Skip All Reviews jobs (Admins and Reviewers).',
  },
  reviewStudio: {
    id: 'navigation.reviewStudio.section',
    defaultMessage: 'Review Studio',
    description: 'Side-navigation section, for Admins, that holds Review Management.',
  },
  management: {
    id: 'navigation.reviewStudio.management',
    defaultMessage: 'Review Management',
    description:
      'Side-navigation link, in the Review Studio section, to the page where review queues are defined and work is assigned (Admins).',
  },
});

type NavIntl = Pick<IntlShape, 'formatMessage'>;

/** The label in the UI's language; without `intl` (callers outside the navigation, tests) the English source text. */
const labelOf = (message: TypedMessageDescriptor<NoMessageValues>, intl?: NavIntl): string =>
  intl ? intl.formatMessage(message) : String(message.defaultMessage);

/** Tasks, with a badge counting the items assigned to the caller. */
export const buildMyTasksNavLink = (count: number | null, intl?: NavIntl): SideNavigationProps.Link => {
  const link: SideNavigationProps.Link = { type: 'link', text: labelOf(messages.tasks, intl), href: `#${MY_TASKS_PATH}` };
  const n = count ?? 0;
  if (n > 0) {
    link.info = (
      <span>
        <Badge color="blue">{n}</Badge>
        <span className="idp-visually-hidden">
          {' '}
          <FormattedMessage {...messages.tasksAssigned} values={{ count: n }} />
        </span>
      </span>
    );
  }
  return link;
};

/**
 * The "Home" section, first in every user's navigation: the Inbox, and for an Annotator
 * (who is assigned HITL work) Tasks and Queues (the queues that list the caller).
 */
export const buildHomeNavSection = (
  inboxLink: SideNavigationProps.Link,
  {
    showWork,
    taskCount,
    showJobs = false,
    intl,
  }: {
    showWork: boolean;
    taskCount: number | null;
    showJobs?: boolean;
    /** Formats the labels; without it the labels are the English source text. */
    intl?: NavIntl;
  },
): SideNavigationProps.Section => ({
  type: 'section',
  text: labelOf(messages.home, intl),
  items: [
    ...(showWork
      ? [
          inboxLink,
          buildMyTasksNavLink(taskCount, intl),
          { type: 'link' as const, text: labelOf(messages.queues, intl), href: `#${MY_QUEUES_PATH}` },
        ]
      : [inboxLink]),
    // Bulk Skip All Reviews jobs (Admins and Reviewers).
    ...(showJobs ? [{ type: 'link' as const, text: labelOf(messages.jobs, intl), href: `#${JOBS_PATH}` }] : []),
  ],
});

/** The "Review Studio" section (Admins): Review Management, where queues are defined and work is assigned. */
export const buildReviewStudioNavSection = (intl?: NavIntl): SideNavigationProps.Section => ({
  type: 'section',
  text: labelOf(messages.reviewStudio, intl),
  items: [{ type: 'link', text: labelOf(messages.management, intl), href: `#${REVIEW_QUEUES_PATH}` }],
});
