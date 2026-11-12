// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import React from 'react';
import { Badge } from '@cloudscape-design/components';
import type { SideNavigationProps } from '@cloudscape-design/components';
import { defineMessages, type IntlShape, type NoMessageValues } from 'react-intl';

import { JOBS_PATH, MY_QUEUES_PATH, MY_TASKS_PATH, REVIEW_QUEUES_PATH } from '../../routes/constants';

export const REVIEW_STUDIO_SECTION = 'Review Studio';
export const HOME_SECTION = 'Home';

const messages = defineMessages<{ jobs: NoMessageValues }>({
  jobs: {
    id: 'navigation.home.jobs',
    defaultMessage: 'Jobs',
    description: 'Side-navigation link under Home to the list of bulk Skip All Reviews jobs (Admins and Reviewers).',
  },
});

/** Tasks, with a badge counting the items assigned to the caller. */
export const buildMyTasksNavLink = (count: number | null): SideNavigationProps.Link => {
  const link: SideNavigationProps.Link = { type: 'link', text: 'Tasks', href: `#${MY_TASKS_PATH}` };
  const n = count ?? 0;
  if (n > 0) {
    link.info = (
      <span>
        <Badge color="blue">{n}</Badge>
        <span className="idp-visually-hidden"> assigned</span>
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
    /** Formats the Jobs label; without it the label is the English source text. */
    intl?: Pick<IntlShape, 'formatMessage'>;
  },
): SideNavigationProps.Section => ({
  type: 'section',
  text: HOME_SECTION,
  items: [
    ...(showWork
      ? [inboxLink, buildMyTasksNavLink(taskCount), { type: 'link' as const, text: 'Queues', href: `#${MY_QUEUES_PATH}` }]
      : [inboxLink]),
    // Bulk Skip All Reviews jobs (Admins and Reviewers).
    ...(showJobs
      ? [{ type: 'link' as const, text: intl ? intl.formatMessage(messages.jobs) : messages.jobs.defaultMessage, href: `#${JOBS_PATH}` }]
      : []),
  ],
});

/** The "Review Studio" section (Admins): Review Management, where queues are defined and work is assigned. */
export const buildReviewStudioNavSection = (): SideNavigationProps.Section => ({
  type: 'section',
  text: REVIEW_STUDIO_SECTION,
  items: [{ type: 'link', text: 'Review Management', href: `#${REVIEW_QUEUES_PATH}` }],
});
