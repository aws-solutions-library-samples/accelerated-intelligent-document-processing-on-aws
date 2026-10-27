// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The info panels (the right-hand panel the info icon opens) of HITL › Queue Management
// and of a queue's page, Production or Test Studio.
import React from 'react';
import { HelpPanel, Icon } from '@cloudscape-design/components';

import { DOCS_BASE_URL } from '../../constants/github';

const LearnMore = (): React.JSX.Element => (
  <div>
    <h3>
      Learn more <Icon name="external" />
    </h3>
    <ul>
      <li>
        <a href={`${DOCS_BASE_URL}/review-queues/`} target="_blank" rel="noopener noreferrer">
          Review queues
        </a>
      </li>
      <li>
        <a href={`${DOCS_BASE_URL}/human-review/`} target="_blank" rel="noopener noreferrer">
          Human review
        </a>
      </li>
      <li>
        <a href={`${DOCS_BASE_URL}/test-studio/`} target="_blank" rel="noopener noreferrer">
          Test Studio
        </a>
      </li>
    </ul>
  </div>
);

export const QueueManagementToolsPanel = (): React.JSX.Element => (
  <HelpPanel header={<h2>Queue Management</h2>} footer={<LearnMore />}>
    <div>
      <p>Every review queue in one place: the Production queues documents are routed to, and one Test Studio queue per test set.</p>
      <h3>Production queues</h3>
      <ul>
        <li>A document whose fields raised confidence alerts goes to the Default queue, when HITL is enabled in its configuration.</li>
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
