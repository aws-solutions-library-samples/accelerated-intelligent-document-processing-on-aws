// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The info panels (the right-hand panel the info icon opens) of Review Studio › Review Management
// and of a queue's page, Production or Test Studio.
import React from 'react';
import { HelpPanel } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, type MessageTag, type NoMessageValues } from 'react-intl';

import LearnMoreLinks from '../common/LearnMoreLinks';

type Bold = { readonly b: MessageTag };

/** Renders a `<b>...</b>` element value: a UI label named inside a sentence. */
const bold = (chunks: React.ReactNode[]): React.JSX.Element => <b>{chunks}</b>;

const messages = defineMessages<{
  linkReviewQueues: NoMessageValues;
  linkHumanReview: NoMessageValues;
  linkTestStudio: NoMessageValues;
  managementTitle: NoMessageValues;
  managementIntro: Bold;
  productionHeading: NoMessageValues;
  productionRouting: NoMessageValues;
  productionSettings: Bold;
  productionOpen: NoMessageValues;
  testStudioHeading: NoMessageValues;
  testStudioSets: NoMessageValues;
  testStudioAnnotators: NoMessageValues;
  queueTitle: NoMessageValues;
  queueIntro: NoMessageValues;
  onThisPage: NoMessageValues;
  queueProgress: Bold;
  keyMetrics: Bold;
  annotators: Bold;
  documentsTable: Bold;
  assignmentsHeading: NoMessageValues;
  assignmentExpiry: NoMessageValues;
  skip: NoMessageValues;
}>({
  linkReviewQueues: {
    id: 'reviewQueues.toolsPanel.linkReviewQueues',
    defaultMessage: 'Review queues',
    description: 'Label of a "Learn more" link to the Review queues documentation page.',
  },
  linkHumanReview: {
    id: 'reviewQueues.toolsPanel.linkHumanReview',
    defaultMessage: 'Human review',
    description: 'Label of a "Learn more" link to the Human review documentation page.',
  },
  linkTestStudio: {
    id: 'reviewQueues.toolsPanel.linkTestStudio',
    defaultMessage: 'Test Studio',
    description: 'Label of a "Learn more" link to the Test Studio documentation page. Test Studio is a product area name.',
  },
  managementTitle: {
    id: 'reviewQueues.toolsPanel.managementTitle',
    defaultMessage: 'Review Management',
    description: 'Title of the info panel of Review Studio › Review Management (the review queues an Admin sets up).',
  },
  managementIntro: {
    id: 'reviewQueues.toolsPanel.managementIntro',
    defaultMessage:
      'Every review queue in one place: the Production queues documents are routed to, and one Test Studio queue per test set. The <b>Production | Test Studio</b> switch chooses which table is shown.',
    description:
      'Opening paragraph of the Review Management info panel. <b>Production | Test Studio</b> names the switch above the tables, in bold: its two options.',
  },
  productionHeading: {
    id: 'reviewQueues.toolsPanel.productionHeading',
    defaultMessage: 'Production queues',
    description: 'Sub-heading in the Review Management info panel, above the notes on production queues.',
  },
  productionRouting: {
    id: 'reviewQueues.toolsPanel.productionRouting',
    defaultMessage:
      'A document whose fields raised confidence alerts goes to the Default queue, when HITL is enabled in its configuration.',
    description:
      'Review Management info panel, "Production queues" item. Default is the name of a queue; HITL (human in the loop) is the name of a configuration setting.',
  },
  productionSettings: {
    id: 'reviewQueues.toolsPanel.productionSettings',
    defaultMessage:
      "<b>Settings</b> sets a queue's name, priority, time limit, eligible Annotators and Enabled switch. A saved change shows at once.",
    description:
      'Review Management info panel, "Production queues" item. <b>Settings</b> is the label of a menu item, shown in bold; Enabled is the label of a switch.',
  },
  productionOpen: {
    id: 'reviewQueues.toolsPanel.productionOpen',
    defaultMessage: 'Open a queue to assign its documents to Annotators, unassign them, move them or remove them from review.',
    description: 'Review Management info panel, "Production queues" item. Annotator is a user role.',
  },
  testStudioHeading: {
    id: 'reviewQueues.toolsPanel.testStudioHeading',
    defaultMessage: 'Test Studio queues',
    description:
      'Sub-heading in the Review Management info panel, above the notes on Test Studio queues. Test Studio is a product area name.',
  },
  testStudioSets: {
    id: 'reviewQueues.toolsPanel.testStudioSets',
    defaultMessage: 'Each test set is a queue: its documents are the labels to check before they become ground truth.',
    description: 'Review Management info panel, "Test Studio queues" item.',
  },
  testStudioAnnotators: {
    id: 'reviewQueues.toolsPanel.testStudioAnnotators',
    defaultMessage: 'List Annotators in its settings; they claim documents or an Admin assigns them.',
    description: 'Review Management info panel, "Test Studio queues" item. Annotator and Admin are user roles.',
  },
  queueTitle: {
    id: 'reviewQueues.toolsPanel.queueTitle',
    defaultMessage: 'Queue',
    description: "Title of the info panel of a review queue's page, Production or Test Studio.",
  },
  queueIntro: {
    id: 'reviewQueues.toolsPanel.queueIntro',
    defaultMessage: "One queue's documents and the Annotators who work them.",
    description: 'Opening paragraph of the Queue info panel. Annotator is a user role.',
  },
  onThisPage: {
    id: 'reviewQueues.toolsPanel.onThisPage',
    defaultMessage: 'On this page',
    description: 'Sub-heading in the Queue info panel, above the list of what the page shows and does.',
  },
  queueProgress: {
    id: 'reviewQueues.toolsPanel.queueProgress',
    defaultMessage:
      '<b>Queue Progress</b>: on a Production queue, the documents assigned or in progress (a finished document leaves the queue); on a Test Studio queue, the documents completed.',
    description: 'Queue info panel, "On this page" item. <b>Queue Progress</b> is the title of a panel of the page, shown in bold.',
  },
  keyMetrics: {
    id: 'reviewQueues.toolsPanel.keyMetrics',
    defaultMessage: "<b>Key Metrics</b>: pending, assigned and in-progress counts, and the queue's Annotators and priority.",
    description: 'Queue info panel, "On this page" item. <b>Key Metrics</b> is the title of a panel of the page, shown in bold.',
  },
  annotators: {
    id: 'reviewQueues.toolsPanel.annotators',
    defaultMessage: '<b>Annotators</b> (Admin): each listed Annotator, with the documents assigned to them.',
    description:
      'Queue info panel, "On this page" item. <b>Annotators</b> is the title of a panel of the page, shown in bold; "(Admin)" says only Admins see it.',
  },
  documentsTable: {
    id: 'reviewQueues.toolsPanel.documentsTable',
    defaultMessage:
      "The documents table: sort by <b>Annotator</b> to see each person's work. Select documents to assign, unassign, move or claim them; open one to edit it.",
    description: 'Queue info panel, "On this page" item. <b>Annotator</b> is the header of a column of the table, shown in bold.',
  },
  assignmentsHeading: {
    id: 'reviewQueues.toolsPanel.assignmentsHeading',
    defaultMessage: 'Assignments',
    description: 'Sub-heading in the Queue info panel, above the notes on how assignments work.',
  },
  assignmentExpiry: {
    id: 'reviewQueues.toolsPanel.assignmentExpiry',
    defaultMessage: "An assignment does not expire. After the queue's time limit, the Annotator is reminded once.",
    description: 'Queue info panel, "Assignments" item. Annotator is a user role.',
  },
  skip: {
    id: 'reviewQueues.toolsPanel.skip',
    defaultMessage: 'Skip gives a document back to the queue, unassigned and not finished; its saved work is kept.',
    description: 'Queue info panel, "Assignments" item. Skip is the label of a button.',
  },
});

const LearnMore = (): React.JSX.Element => (
  <LearnMoreLinks
    links={[
      { text: <FormattedMessage {...messages.linkReviewQueues} />, docsPath: 'review-queues/' },
      { text: <FormattedMessage {...messages.linkHumanReview} />, docsPath: 'human-review/' },
      { text: <FormattedMessage {...messages.linkTestStudio} />, docsPath: 'test-studio/' },
    ]}
  />
);

export const ReviewManagementToolsPanel = (): React.JSX.Element => (
  <HelpPanel
    header={
      <h2>
        <FormattedMessage {...messages.managementTitle} />
      </h2>
    }
    footer={<LearnMore />}
  >
    <div>
      <p>
        <FormattedMessage {...messages.managementIntro} values={{ b: bold }} />
      </p>
      <h3>
        <FormattedMessage {...messages.productionHeading} />
      </h3>
      <ul>
        <li>
          <FormattedMessage {...messages.productionRouting} />
        </li>
        <li>
          <FormattedMessage {...messages.productionSettings} values={{ b: bold }} />
        </li>
        <li>
          <FormattedMessage {...messages.productionOpen} />
        </li>
      </ul>
      <h3>
        <FormattedMessage {...messages.testStudioHeading} />
      </h3>
      <ul>
        <li>
          <FormattedMessage {...messages.testStudioSets} />
        </li>
        <li>
          <FormattedMessage {...messages.testStudioAnnotators} />
        </li>
      </ul>
    </div>
  </HelpPanel>
);

export const QueueToolsPanel = (): React.JSX.Element => (
  <HelpPanel
    header={
      <h2>
        <FormattedMessage {...messages.queueTitle} />
      </h2>
    }
    footer={<LearnMore />}
  >
    <div>
      <p>
        <FormattedMessage {...messages.queueIntro} />
      </p>
      <h3>
        <FormattedMessage {...messages.onThisPage} />
      </h3>
      <ul>
        <li>
          <FormattedMessage {...messages.queueProgress} values={{ b: bold }} />
        </li>
        <li>
          <FormattedMessage {...messages.keyMetrics} values={{ b: bold }} />
        </li>
        <li>
          <FormattedMessage {...messages.annotators} values={{ b: bold }} />
        </li>
        <li>
          <FormattedMessage {...messages.documentsTable} values={{ b: bold }} />
        </li>
      </ul>
      <h3>
        <FormattedMessage {...messages.assignmentsHeading} />
      </h3>
      <ul>
        <li>
          <FormattedMessage {...messages.assignmentExpiry} />
        </li>
        <li>
          <FormattedMessage {...messages.skip} />
        </li>
      </ul>
    </div>
  </HelpPanel>
);
