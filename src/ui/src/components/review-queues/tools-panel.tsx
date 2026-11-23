// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The info panels (the right-hand panel the info icon opens) of Review Studio › Review Management
// and of a queue's page, Production or Test Studio.
import React from 'react';
import { HelpPanel } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, useIntl, type MessageTag, type NoMessageValues } from 'react-intl';

import LearnMoreLinks from '../common/LearnMoreLinks';

const messages = defineMessages<{
  reviewRoutingLink: NoMessageValues;
  routing: NoMessageValues;
  defaultQueue: NoMessageValues;
  createAndDelete: { readonly b: MessageTag };
}>({
  reviewRoutingLink: {
    id: 'reviewQueues.toolsPanel.reviewRoutingLink',
    defaultMessage: 'Review routing',
    description: 'Learn more link in the Review Management info panels: the documentation page about routing rules.',
  },
  routing: {
    id: 'reviewQueues.toolsPanel.routing',
    defaultMessage:
      "While Review Routing is on in a document's configuration profile and the Default queue is enabled, the routing rules of that profile decide whether the document is reviewed and send it to the queue a routing rule names. A warning on this page says when routing rules are not evaluated, and why.",
    description:
      'Review Management info panel, Production queues: how a document reaches a queue. Review Routing is the name of a processing step and of its switch in a configuration profile.',
  },
  defaultQueue: {
    id: 'reviewQueues.toolsPanel.defaultQueue',
    defaultMessage:
      'The Default queue receives the documents the built-in routing rules hold (failed extractions, confidence alerts, missing required fields) and documents whose queue is missing or disabled.',
    description: 'Review Management info panel, Production queues: what the Default queue receives.',
  },
  createAndDelete: {
    id: 'reviewQueues.toolsPanel.createAndDelete',
    defaultMessage:
      '<b>Create queue</b> adds a queue that routing rules can name by its id. Deleting a queue moves its documents to the Default queue, and lists the routing rules that name it first.',
    description: 'Review Management info panel, Production queues: creating and deleting a queue. <b> marks the name of the button.',
  },
});

const bold = (chunks: React.ReactNode[]): React.JSX.Element => <b>{chunks}</b>;

const LearnMore = (): React.JSX.Element => {
  const intl = useIntl();
  return (
    <LearnMoreLinks
      links={[
        { text: 'Review queues', docsPath: 'review-queues/' },
        { text: intl.formatMessage(messages.reviewRoutingLink), docsPath: 'review-routing/' },
        { text: 'Human review', docsPath: 'human-review/' },
        { text: 'Test Studio', docsPath: 'test-studio/' },
      ]}
    />
  );
};

export const ReviewManagementToolsPanel = (): React.JSX.Element => (
  <HelpPanel header={<h2>Review Management</h2>} footer={<LearnMore />}>
    <div>
      <p>
        Every review queue in one place: the Production queues documents are routed to, and one Test Studio queue per test set. The{' '}
        <b>Production | Test Studio</b> switch chooses which table is shown.
      </p>
      <h3>Production queues</h3>
      <ul>
        <li>
          <FormattedMessage {...messages.routing} />
        </li>
        <li>
          <FormattedMessage {...messages.defaultQueue} />
        </li>
        <li>
          <FormattedMessage {...messages.createAndDelete} values={{ b: bold }} />
        </li>
        <li>
          <b>Settings</b> sets a queue&apos;s name, priority, time limit, eligible Annotators and Enabled switch. A saved change shows at
          once.
        </li>
        <li>Open a queue to assign its documents to Annotators, unassign them, move them or remove them from review.</li>
      </ul>
      <h3>Test Studio queues</h3>
      <ul>
        <li>Each test set is a queue: its documents are the labels to check before they become ground truth.</li>
        <li>List Annotators in its settings; they claim documents or an Admin assigns them.</li>
      </ul>
    </div>
  </HelpPanel>
);

export const QueueToolsPanel = (): React.JSX.Element => (
  <HelpPanel header={<h2>Queue</h2>} footer={<LearnMore />}>
    <div>
      <p>One queue&apos;s documents and the Annotators who work them.</p>
      <h3>On this page</h3>
      <ul>
        <li>
          <b>Queue Progress</b>: on a Production queue, the documents assigned or in progress (a finished document leaves the queue); on a
          Test Studio queue, the documents completed.
        </li>
        <li>
          <b>Key Metrics</b>: pending, assigned and in-progress counts, and the queue&apos;s Annotators and priority.
        </li>
        <li>
          <b>Annotators</b> (Admin): each listed Annotator, with the documents assigned to them.
        </li>
        <li>
          The documents table: sort by <b>Annotator</b> to see each person&apos;s work. Select documents to assign, unassign, move or claim
          them; open one to edit it.
        </li>
      </ul>
      <h3>Assignments</h3>
      <ul>
        <li>An assignment does not expire. After the queue&apos;s time limit, the Annotator is reminded once.</li>
        <li>Skip gives a document back to the queue, unassigned and not finished; its saved work is kept.</li>
      </ul>
    </div>
  </HelpPanel>
);
