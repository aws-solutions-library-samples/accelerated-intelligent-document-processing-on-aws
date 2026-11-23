// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The info panels (the right-hand panel the info icon opens) of the Home pages:
// Inbox, Tasks, Queues, Jobs and a job's page. Each says what the page is for, who
// uses it and where it is reached from, and links to the docs.
import React from 'react';
import { HelpPanel } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, type MessageTag, type NoMessageValues } from 'react-intl';

import LearnMoreLinks, { type DocLink } from '../common/LearnMoreLinks';

type Bold = { readonly b: MessageTag };

/** Renders a `<b>...</b>` element value: a UI label named inside a sentence. */
const bold = (chunks: React.ReactNode[]): React.JSX.Element => <b>{chunks}</b>;

const messages = defineMessages<{
  whoUsesIt: NoMessageValues;
  onThisPage: NoMessageValues;
  goodToKnow: NoMessageValues;
  linkReviewQueues: NoMessageValues;
  linkHumanReview: NoMessageValues;
  linkTestStudio: NoMessageValues;
  inboxTitle: NoMessageValues;
  inboxLinkInbox: NoMessageValues;
  inboxLinkRetention: NoMessageValues;
  inboxIntro: NoMessageValues;
  inboxWho: NoMessageValues;
  inboxOpen: Bold;
  inboxMark: Bold;
  inboxSettings: NoMessageValues;
  inboxRetention: NoMessageValues;
  inboxLookingAhead: NoMessageValues;
  inboxLookingAheadText: NoMessageValues;
  tasksTitle: NoMessageValues;
  tasksIntro: NoMessageValues;
  tasksWho: NoMessageValues;
  tasksOpen: NoMessageValues;
  tasksSkip: Bold;
  tasksClaimMore: NoMessageValues;
  queuesTitle: NoMessageValues;
  queuesIntro: NoMessageValues;
  queuesWho: NoMessageValues;
  queuesCounts: NoMessageValues;
  queuesClaim: Bold;
  queuesSkipAll: Bold;
  jobsLinkSkipAll: NoMessageValues;
  jobsLinkLimit: NoMessageValues;
  jobsTitle: NoMessageValues;
  jobsIntro: NoMessageValues;
  jobsWhoRoles: NoMessageValues;
  jobsWhoStarted: Bold;
  jobsColumns: NoMessageValues;
  jobsOpen: NoMessageValues;
  jobsOneAtATime: NoMessageValues;
  jobsRetention: NoMessageValues;
  jobTitle: NoMessageValues;
  jobIntro: NoMessageValues;
  jobRequest: NoMessageValues;
  jobProgress: NoMessageValues;
  jobResults: NoMessageValues;
  jobOutcomes: NoMessageValues;
  jobOutcomeSkipped: Bold;
  jobOutcomeLeftAlone: Bold;
  jobOutcomeRetry: Bold;
}>({
  whoUsesIt: {
    id: 'home.toolsPanels.whoUsesIt',
    defaultMessage: 'Who uses it',
    description: 'Sub-heading in a Home info panel, above the list of roles that use the page.',
  },
  onThisPage: {
    id: 'home.toolsPanels.onThisPage',
    defaultMessage: 'On this page',
    description: 'Sub-heading in a Home info panel, above the list of what the page shows and does.',
  },
  goodToKnow: {
    id: 'home.toolsPanels.goodToKnow',
    defaultMessage: 'Good to know',
    description: 'Sub-heading in a Home info panel, above a list of notes and limits.',
  },
  linkReviewQueues: {
    id: 'home.toolsPanels.linkReviewQueues',
    defaultMessage: 'Review queues',
    description: 'Label of a "Learn more" link to the Review queues documentation page.',
  },
  linkHumanReview: {
    id: 'home.toolsPanels.linkHumanReview',
    defaultMessage: 'Human review',
    description: 'Label of a "Learn more" link to the Human review documentation page.',
  },
  linkTestStudio: {
    id: 'home.toolsPanels.linkTestStudio',
    defaultMessage: 'Test Studio',
    description: 'Label of a "Learn more" link to the Test Studio documentation page. Test Studio is a product area name.',
  },
  inboxTitle: {
    id: 'home.toolsPanels.inboxTitle',
    defaultMessage: 'Inbox',
    description: 'Title of the info panel of the Home › Inbox page (the messages the platform sends the user).',
  },
  inboxLinkInbox: {
    id: 'home.toolsPanels.inboxLinkInbox',
    defaultMessage: 'Inbox',
    description: 'Label of a "Learn more" link to the Inbox section of the web UI documentation.',
  },
  inboxLinkRetention: {
    id: 'home.toolsPanels.inboxLinkRetention',
    defaultMessage: 'Inbox message retention',
    description: 'Label of a "Learn more" link to the documentation of how long Inbox messages are kept.',
  },
  inboxIntro: {
    id: 'home.toolsPanels.inboxIntro',
    defaultMessage:
      'Messages for you, or for a group you belong to, from the platform: review work waiting, assignments, claims about to expire or released, and finished bulk review jobs.',
    description: 'Opening paragraph of the Inbox info panel: what the Inbox holds.',
  },
  inboxWho: {
    id: 'home.toolsPanels.inboxWho',
    defaultMessage: 'Every user. The bell in the top bar and Home › Inbox show the unread count.',
    description: 'Inbox info panel, "Who uses it" item. "Home › Inbox" is the navigation path to the page.',
  },
  inboxOpen: {
    id: 'home.toolsPanels.inboxOpen',
    defaultMessage: 'Open a message to read it; <b>Open</b> goes to the page it is about.',
    description: 'Inbox info panel, "On this page" item. <b>Open</b> is the label of a button, shown in bold.',
  },
  inboxMark: {
    id: 'home.toolsPanels.inboxMark',
    defaultMessage: 'Mark messages read or unread, or delete them. <b>More actions</b> marks all as read or clears the read ones.',
    description: 'Inbox info panel, "On this page" item. <b>More actions</b> is the label of a menu button, shown in bold.',
  },
  inboxSettings: {
    id: 'home.toolsPanels.inboxSettings',
    defaultMessage: 'The settings icon chooses which message types you receive.',
    description: 'Inbox info panel, "On this page" item about the settings (gear) icon.',
  },
  inboxRetention: {
    id: 'home.toolsPanels.inboxRetention',
    defaultMessage:
      'Messages are kept for a limited time and up to a per-user cap (Configuration › Processing Settings › HITL › Inbox messages).',
    description: 'Inbox info panel, "Good to know" item. The parenthesis is the navigation path to the setting.',
  },
  inboxLookingAhead: {
    id: 'home.toolsPanels.inboxLookingAhead',
    defaultMessage: 'Looking ahead',
    description: 'Sub-heading in the Inbox info panel, above a paragraph on planned capabilities.',
  },
  inboxLookingAheadText: {
    id: 'home.toolsPanels.inboxLookingAheadText',
    defaultMessage:
      'The Inbox is planned to become the general notification channel for the platform. As new capabilities are added, it will also carry updates from background jobs, the outcome of asynchronous requests, and reminders when a task is due.',
    description: 'Inbox info panel, paragraph under "Looking ahead".',
  },
  tasksTitle: {
    id: 'home.toolsPanels.tasksTitle',
    defaultMessage: 'Tasks',
    description: 'Title of the info panel of the Home › Tasks page (review work the user holds).',
  },
  tasksIntro: {
    id: 'home.toolsPanels.tasksIntro',
    defaultMessage:
      'The review work you hold: production queue items you claimed or an Admin assigned to you, and Test Studio documents to annotate.',
    description: 'Opening paragraph of the Tasks info panel. "Admin" is a user role.',
  },
  tasksWho: {
    id: 'home.toolsPanels.tasksWho',
    defaultMessage: 'Annotators and Reviewers. The badge on Home › Tasks counts the items assigned to you.',
    description: 'Tasks info panel, "Who uses it" item. Annotators and Reviewers are user roles.',
  },
  tasksOpen: {
    id: 'home.toolsPanels.tasksOpen',
    defaultMessage: 'Open an item to review or annotate it in the edit page.',
    description: 'Tasks info panel, "On this page" item.',
  },
  tasksSkip: {
    id: 'home.toolsPanels.tasksSkip',
    defaultMessage: '<b>Skip</b> on an item hands it back to its queue unfinished; your saved work is kept.',
    description: 'Tasks info panel, "On this page" item. <b>Skip</b> is the label of a button, shown in bold.',
  },
  tasksClaimMore: {
    id: 'home.toolsPanels.tasksClaimMore',
    defaultMessage: 'Claim more work from Home › Queues.',
    description: 'Tasks info panel, "On this page" item. "Home › Queues" is the navigation path to a page.',
  },
  queuesTitle: {
    id: 'home.toolsPanels.queuesTitle',
    defaultMessage: 'Queues',
    description: 'Title of the info panel of the Home › Queues page (the review queues the user works).',
  },
  queuesIntro: {
    id: 'home.toolsPanels.queuesIntro',
    defaultMessage:
      'The review queues you work: every Production queue for a Reviewer, and the queues (Production or Test Studio) that list you as an Annotator.',
    description:
      'Opening paragraph of the Queues info panel. Reviewer and Annotator are user roles; Production and Test Studio are kinds of queue.',
  },
  queuesWho: {
    id: 'home.toolsPanels.queuesWho',
    defaultMessage: 'Annotators and Reviewers. Admins manage queues in Review Studio › Review Management.',
    description: 'Queues info panel, "Who uses it" item. "Review Studio › Review Management" is a navigation path.',
  },
  queuesCounts: {
    id: 'home.toolsPanels.queuesCounts',
    defaultMessage: 'Each queue with its pending and claimed counts. Open a queue to see its items.',
    description: 'Queues info panel, "On this page" item.',
  },
  queuesClaim: {
    id: 'home.toolsPanels.queuesClaim',
    defaultMessage: "On a queue's page, claim an item (or choose <b>Next item</b>) to work it in Home › Tasks.",
    description: 'Queues info panel, "On this page" item. <b>Next item</b> is the label of a button, shown in bold.',
  },
  queuesSkipAll: {
    id: 'home.toolsPanels.queuesSkipAll',
    defaultMessage: "Reviewers can also finish waiting items without reviewing them with <b>Skip All Reviews</b> on a queue's page.",
    description: 'Queues info panel, "On this page" item. <b>Skip All Reviews</b> is the label of a button, shown in bold.',
  },
  jobsLinkSkipAll: {
    id: 'home.toolsPanels.jobsLinkSkipAll',
    defaultMessage: 'Skip All Reviews for many documents',
    description: 'Label of a "Learn more" link to the documentation of bulk Skip All Reviews.',
  },
  jobsLinkLimit: {
    id: 'home.toolsPanels.jobsLinkLimit',
    defaultMessage: 'Bulk skip synchronous limit',
    description:
      'Label of a "Learn more" link to the documentation of the setting that caps how many documents a bulk skip handles at once.',
  },
  jobsTitle: {
    id: 'home.toolsPanels.jobsTitle',
    defaultMessage: 'Jobs',
    description: 'Title of the info panel of the Home › Jobs page (bulk Skip All Reviews runs).',
  },
  jobsIntro: {
    id: 'home.toolsPanels.jobsIntro',
    defaultMessage: 'Every bulk Skip All Reviews run: those that ran at once and those that ran as a background job.',
    description: 'Opening paragraph of the Jobs info panel. "Skip All Reviews" is the name of an action.',
  },
  jobsWhoRoles: {
    id: 'home.toolsPanels.jobsWhoRoles',
    defaultMessage: "Admins (every user's jobs) and Reviewers (their own).",
    description: 'Jobs info panel, "Who uses it" item. Admins and Reviewers are user roles.',
  },
  jobsWhoStarted: {
    id: 'home.toolsPanels.jobsWhoStarted',
    defaultMessage: "Runs are started from the Document List (<b>Review › Skip All Reviews</b>) or a production queue's page.",
    description: 'Jobs info panel, "Who uses it" item. <b>Review › Skip All Reviews</b> is a menu path, shown in bold.',
  },
  jobsColumns: {
    id: 'home.toolsPanels.jobsColumns',
    defaultMessage:
      "Each job's type, status, mode, profile, when it was created and finished, and how many documents it processed and skipped.",
    description: 'Jobs info panel, "On this page" item listing the columns of the jobs table.',
  },
  jobsOpen: {
    id: 'home.toolsPanels.jobsOpen',
    defaultMessage: 'Open a job to see its progress and per-document results. The list refreshes while a job is queued or running.',
    description: 'Jobs info panel, "On this page" item.',
  },
  jobsOneAtATime: {
    id: 'home.toolsPanels.jobsOneAtATime',
    defaultMessage: 'One background job runs at a time per stack; others wait as Queued.',
    description: 'Jobs info panel, "Good to know" item. "Queued" is a job status name.',
  },
  jobsRetention: {
    id: 'home.toolsPanels.jobsRetention',
    defaultMessage:
      "Jobs are kept for 30 days, or for the stack's data retention if that is shorter. The Inbox tells you when a background job finishes.",
    description: 'Jobs info panel, "Good to know" item about how long jobs are kept.',
  },
  jobTitle: {
    id: 'home.toolsPanels.jobTitle',
    defaultMessage: 'Job',
    description: 'Title of the info panel of the page of one job (a single bulk Skip All Reviews run).',
  },
  jobIntro: {
    id: 'home.toolsPanels.jobIntro',
    defaultMessage: 'One bulk Skip All Reviews run: what was asked, its progress, and what happened to each document.',
    description: 'Opening paragraph of the Job info panel.',
  },
  jobRequest: {
    id: 'home.toolsPanels.jobRequest',
    defaultMessage:
      'The request: mode (selected documents or by profile), profile and submitted-date range, reason, note and whether documents are resubmitted.',
    description: 'Job info panel, "On this page" item describing the request summary.',
  },
  jobProgress: {
    id: 'home.toolsPanels.jobProgress',
    defaultMessage: 'Progress and the count per outcome, refreshed while the job is queued or running.',
    description: 'Job info panel, "On this page" item.',
  },
  jobResults: {
    id: 'home.toolsPanels.jobResults',
    defaultMessage: 'The results table: filter by outcome, open a document, or export to Excel.',
    description: 'Job info panel, "On this page" item.',
  },
  jobOutcomes: {
    id: 'home.toolsPanels.jobOutcomes',
    defaultMessage: 'Outcomes',
    description: 'Sub-heading in the Job info panel, above the list of per-document outcome names and their meaning.',
  },
  jobOutcomeSkipped: {
    id: 'home.toolsPanels.jobOutcomeSkipped',
    defaultMessage:
      "<b>Skipped</b>: the review was finished without anyone checking it, recorded with the reason in the document's review history.",
    description: 'Job info panel, outcome item. <b>Skipped</b> is an outcome name shown in the results table, in bold.',
  },
  jobOutcomeLeftAlone: {
    id: 'home.toolsPanels.jobOutcomeLeftAlone',
    defaultMessage:
      '<b>Held by another reviewer</b>, <b>Already finished</b>, <b>Not pending</b>, <b>Test Studio excluded</b> and <b>Not permitted</b>: left alone.',
    description: 'Job info panel, outcome item. Each bold phrase is an outcome name shown in the results table.',
  },
  jobOutcomeRetry: {
    id: 'home.toolsPanels.jobOutcomeRetry',
    defaultMessage:
      '<b>Resubmit failed</b>: skipped, but its downstream run could not be queued. <b>Failed</b> and <b>Not attempted</b>: try again.',
    description: 'Job info panel, outcome item. Each bold phrase is an outcome name shown in the results table.',
  },
});

const REVIEW_QUEUES_LINK: DocLink = { text: <FormattedMessage {...messages.linkReviewQueues} />, docsPath: 'review-queues/' };
const HUMAN_REVIEW_LINK: DocLink = { text: <FormattedMessage {...messages.linkHumanReview} />, docsPath: 'human-review/' };
const TEST_STUDIO_LINK: DocLink = { text: <FormattedMessage {...messages.linkTestStudio} />, docsPath: 'test-studio/' };

const INBOX_LINKS: DocLink[] = [
  { text: <FormattedMessage {...messages.inboxLinkInbox} />, docsPath: 'web-ui/#inbox' },
  { text: <FormattedMessage {...messages.inboxLinkRetention} />, docsPath: 'configuration/#inbox-message-retention' },
  REVIEW_QUEUES_LINK,
];

const JOBS_LINKS: DocLink[] = [
  { text: <FormattedMessage {...messages.jobsLinkSkipAll} />, docsPath: 'human-review/#skip-all-reviews-for-many-documents' },
  { text: <FormattedMessage {...messages.jobsLinkLimit} />, docsPath: 'configuration/#bulk-skip-all-reviews-limit' },
  REVIEW_QUEUES_LINK,
];

export const InboxToolsPanel = (): React.JSX.Element => (
  <HelpPanel
    header={
      <h2>
        <FormattedMessage {...messages.inboxTitle} />
      </h2>
    }
    footer={<LearnMoreLinks links={INBOX_LINKS} />}
  >
    <div>
      <p>
        <FormattedMessage {...messages.inboxIntro} />
      </p>
      <h3>
        <FormattedMessage {...messages.whoUsesIt} />
      </h3>
      <ul>
        <li>
          <FormattedMessage {...messages.inboxWho} />
        </li>
      </ul>
      <h3>
        <FormattedMessage {...messages.onThisPage} />
      </h3>
      <ul>
        <li>
          <FormattedMessage {...messages.inboxOpen} values={{ b: bold }} />
        </li>
        <li>
          <FormattedMessage {...messages.inboxMark} values={{ b: bold }} />
        </li>
        <li>
          <FormattedMessage {...messages.inboxSettings} />
        </li>
      </ul>
      <h3>
        <FormattedMessage {...messages.goodToKnow} />
      </h3>
      <ul>
        <li>
          <FormattedMessage {...messages.inboxRetention} />
        </li>
      </ul>
      <h3>
        <FormattedMessage {...messages.inboxLookingAhead} />
      </h3>
      <p>
        <FormattedMessage {...messages.inboxLookingAheadText} />
      </p>
    </div>
  </HelpPanel>
);

export const TasksToolsPanel = (): React.JSX.Element => (
  <HelpPanel
    header={
      <h2>
        <FormattedMessage {...messages.tasksTitle} />
      </h2>
    }
    footer={<LearnMoreLinks links={[REVIEW_QUEUES_LINK, HUMAN_REVIEW_LINK, TEST_STUDIO_LINK]} />}
  >
    <div>
      <p>
        <FormattedMessage {...messages.tasksIntro} />
      </p>
      <h3>
        <FormattedMessage {...messages.whoUsesIt} />
      </h3>
      <ul>
        <li>
          <FormattedMessage {...messages.tasksWho} />
        </li>
      </ul>
      <h3>
        <FormattedMessage {...messages.onThisPage} />
      </h3>
      <ul>
        <li>
          <FormattedMessage {...messages.tasksOpen} />
        </li>
        <li>
          <FormattedMessage {...messages.tasksSkip} values={{ b: bold }} />
        </li>
        <li>
          <FormattedMessage {...messages.tasksClaimMore} />
        </li>
      </ul>
    </div>
  </HelpPanel>
);

export const MyQueuesToolsPanel = (): React.JSX.Element => (
  <HelpPanel
    header={
      <h2>
        <FormattedMessage {...messages.queuesTitle} />
      </h2>
    }
    footer={<LearnMoreLinks links={[REVIEW_QUEUES_LINK, HUMAN_REVIEW_LINK]} />}
  >
    <div>
      <p>
        <FormattedMessage {...messages.queuesIntro} />
      </p>
      <h3>
        <FormattedMessage {...messages.whoUsesIt} />
      </h3>
      <ul>
        <li>
          <FormattedMessage {...messages.queuesWho} />
        </li>
      </ul>
      <h3>
        <FormattedMessage {...messages.onThisPage} />
      </h3>
      <ul>
        <li>
          <FormattedMessage {...messages.queuesCounts} />
        </li>
        <li>
          <FormattedMessage {...messages.queuesClaim} values={{ b: bold }} />
        </li>
        <li>
          <FormattedMessage {...messages.queuesSkipAll} values={{ b: bold }} />
        </li>
      </ul>
    </div>
  </HelpPanel>
);

export const JobsToolsPanel = (): React.JSX.Element => (
  <HelpPanel
    header={
      <h2>
        <FormattedMessage {...messages.jobsTitle} />
      </h2>
    }
    footer={<LearnMoreLinks links={JOBS_LINKS} />}
  >
    <div>
      <p>
        <FormattedMessage {...messages.jobsIntro} />
      </p>
      <h3>
        <FormattedMessage {...messages.whoUsesIt} />
      </h3>
      <ul>
        <li>
          <FormattedMessage {...messages.jobsWhoRoles} />
        </li>
        <li>
          <FormattedMessage {...messages.jobsWhoStarted} values={{ b: bold }} />
        </li>
      </ul>
      <h3>
        <FormattedMessage {...messages.onThisPage} />
      </h3>
      <ul>
        <li>
          <FormattedMessage {...messages.jobsColumns} />
        </li>
        <li>
          <FormattedMessage {...messages.jobsOpen} />
        </li>
      </ul>
      <h3>
        <FormattedMessage {...messages.goodToKnow} />
      </h3>
      <ul>
        <li>
          <FormattedMessage {...messages.jobsOneAtATime} />
        </li>
        <li>
          <FormattedMessage {...messages.jobsRetention} />
        </li>
      </ul>
    </div>
  </HelpPanel>
);

export const JobToolsPanel = (): React.JSX.Element => (
  <HelpPanel
    header={
      <h2>
        <FormattedMessage {...messages.jobTitle} />
      </h2>
    }
    footer={<LearnMoreLinks links={JOBS_LINKS} />}
  >
    <div>
      <p>
        <FormattedMessage {...messages.jobIntro} />
      </p>
      <h3>
        <FormattedMessage {...messages.onThisPage} />
      </h3>
      <ul>
        <li>
          <FormattedMessage {...messages.jobRequest} />
        </li>
        <li>
          <FormattedMessage {...messages.jobProgress} />
        </li>
        <li>
          <FormattedMessage {...messages.jobResults} />
        </li>
      </ul>
      <h3>
        <FormattedMessage {...messages.jobOutcomes} />
      </h3>
      <ul>
        <li>
          <FormattedMessage {...messages.jobOutcomeSkipped} values={{ b: bold }} />
        </li>
        <li>
          <FormattedMessage {...messages.jobOutcomeLeftAlone} values={{ b: bold }} />
        </li>
        <li>
          <FormattedMessage {...messages.jobOutcomeRetry} values={{ b: bold }} />
        </li>
      </ul>
    </div>
  </HelpPanel>
);
