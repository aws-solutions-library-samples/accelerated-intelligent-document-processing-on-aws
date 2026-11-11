// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The info panels (the right-hand panel the info icon opens) of the Home pages:
// Inbox, Tasks, Queues, Jobs and a job's page. Each says what the page is for, who
// uses it and where it is reached from, and links to the docs.
import React from 'react';
import { HelpPanel } from '@cloudscape-design/components';

import LearnMoreLinks from '../common/LearnMoreLinks';

export const InboxToolsPanel = (): React.JSX.Element => (
  <HelpPanel
    header={<h2>Inbox</h2>}
    footer={
      <LearnMoreLinks
        links={[
          { text: 'Inbox', path: 'web-ui/#inbox' },
          { text: 'Inbox message retention', path: 'configuration/#inbox-message-retention' },
          { text: 'Review queues', path: 'review-queues/' },
        ]}
      />
    }
  >
    <div>
      <p>
        Messages for you, or for a group you belong to, from the platform: review work waiting, assignments, claims about to expire or
        released, and finished bulk review jobs.
      </p>
      <h3>Who uses it</h3>
      <ul>
        <li>Every user. The bell in the top bar and Home › Inbox show the unread count.</li>
      </ul>
      <h3>On this page</h3>
      <ul>
        <li>
          Open a message to read it; <b>Open</b> goes to the page it is about.
        </li>
        <li>
          Mark messages read or unread, or delete them. <b>More actions</b> marks all as read or clears the read ones.
        </li>
        <li>The settings icon chooses which message types you receive.</li>
      </ul>
      <h3>Good to know</h3>
      <ul>
        <li>
          Messages are kept for a limited time and up to a per-user cap (Configuration › Processing Settings › HITL › Inbox messages).
        </li>
      </ul>
      <h3>Looking ahead</h3>
      <p>
        The Inbox is planned to become the general notification channel for the platform. As new capabilities are added, it will also carry
        updates from background jobs, the outcome of asynchronous requests, and reminders when a task is due.
      </p>
    </div>
  </HelpPanel>
);

export const TasksToolsPanel = (): React.JSX.Element => (
  <HelpPanel
    header={<h2>Tasks</h2>}
    footer={
      <LearnMoreLinks
        links={[
          { text: 'Review queues', path: 'review-queues/' },
          { text: 'Human review', path: 'human-review/' },
          { text: 'Test Studio', path: 'test-studio/' },
        ]}
      />
    }
  >
    <div>
      <p>
        The review work you hold: production queue items you claimed or an Admin assigned to you, and Test Studio documents to annotate.
      </p>
      <h3>Who uses it</h3>
      <ul>
        <li>Annotators and Reviewers. The badge on Home › Tasks counts the items assigned to you.</li>
      </ul>
      <h3>On this page</h3>
      <ul>
        <li>Open an item to review or annotate it in the edit page.</li>
        <li>
          <b>Skip</b> on an item hands it back to its queue unfinished; your saved work is kept.
        </li>
        <li>Claim more work from Home › Queues.</li>
      </ul>
    </div>
  </HelpPanel>
);

export const MyQueuesToolsPanel = (): React.JSX.Element => (
  <HelpPanel
    header={<h2>Queues</h2>}
    footer={
      <LearnMoreLinks
        links={[
          { text: 'Review queues', path: 'review-queues/' },
          { text: 'Human review', path: 'human-review/' },
        ]}
      />
    }
  >
    <div>
      <p>
        The review queues you work: every Production queue for a Reviewer, and the queues (Production or Test Studio) that list you as an
        Annotator.
      </p>
      <h3>Who uses it</h3>
      <ul>
        <li>Annotators and Reviewers. Admins manage queues in Review Studio › Review Management.</li>
      </ul>
      <h3>On this page</h3>
      <ul>
        <li>Each queue with its pending and claimed counts. Open a queue to see its items.</li>
        <li>
          On a queue&apos;s page, claim an item (or choose <b>Next item</b>) to work it in Home › Tasks.
        </li>
        <li>
          Reviewers can also finish waiting items without reviewing them with <b>Skip All Reviews</b> on a queue&apos;s page.
        </li>
      </ul>
    </div>
  </HelpPanel>
);

const JOBS_LINKS = [
  { text: 'Skip All Reviews for many documents', path: 'human-review/#skip-all-reviews-for-many-documents' },
  { text: 'Bulk skip synchronous limit', path: 'configuration/#bulk-skip-all-reviews-limit' },
  { text: 'Review queues', path: 'review-queues/' },
];

export const JobsToolsPanel = (): React.JSX.Element => (
  <HelpPanel header={<h2>Jobs</h2>} footer={<LearnMoreLinks links={JOBS_LINKS} />}>
    <div>
      <p>Every bulk Skip All Reviews run: those that ran at once and those that ran as a background job.</p>
      <h3>Who uses it</h3>
      <ul>
        <li>Admins (every user&apos;s jobs) and Reviewers (their own).</li>
        <li>
          Runs are started from the Document List (<b>Review › Skip All Reviews</b>) or a production queue&apos;s page.
        </li>
      </ul>
      <h3>On this page</h3>
      <ul>
        <li>
          Each job&apos;s type, status, mode, profile, when it was created and finished, and how many documents it processed and skipped.
        </li>
        <li>Open a job to see its progress and per-document results. The list refreshes while a job is queued or running.</li>
      </ul>
      <h3>Good to know</h3>
      <ul>
        <li>One background job runs at a time per stack; others wait as Queued.</li>
        <li>
          Jobs are kept for 30 days, or for the stack&apos;s data retention if that is shorter. The Inbox tells you when a background job
          finishes.
        </li>
      </ul>
    </div>
  </HelpPanel>
);

export const JobToolsPanel = (): React.JSX.Element => (
  <HelpPanel header={<h2>Job</h2>} footer={<LearnMoreLinks links={JOBS_LINKS} />}>
    <div>
      <p>One bulk Skip All Reviews run: what was asked, its progress, and what happened to each document.</p>
      <h3>On this page</h3>
      <ul>
        <li>
          The request: mode (selected documents or by profile), profile and submitted-date range, reason, note and whether documents are
          resubmitted.
        </li>
        <li>Progress and the count per outcome, refreshed while the job is queued or running.</li>
        <li>The results table: filter by outcome, open a document, or export to Excel.</li>
      </ul>
      <h3>Outcomes</h3>
      <ul>
        <li>
          <b>Skipped</b>: the review was finished without anyone checking it, recorded with the reason in the document&apos;s review
          history.
        </li>
        <li>
          <b>Held by another reviewer</b>, <b>Already finished</b>, <b>Not pending</b>, <b>Test Studio excluded</b> and <b>Not permitted</b>
          : left alone.
        </li>
        <li>
          <b>Resubmit failed</b>: skipped, but its downstream run could not be queued. <b>Failed</b> and <b>Not attempted</b>: try again.
        </li>
      </ul>
    </div>
  </HelpPanel>
);
