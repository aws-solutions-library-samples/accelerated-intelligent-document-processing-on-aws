// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React from 'react';
import { HelpPanel, Icon } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, type NoMessageValues } from 'react-intl';

import FeedbackHelpSection from '../common/feedback-help-section';

const DOCS_BASE_URL = 'https://aws-solutions-library-samples.github.io/accelerated-intelligent-document-processing-on-aws';

const messages = defineMessages<{
  title: NoMessageValues;
  learnMore: NoMessageValues;
  docsWebUi: NoMessageValues;
  docsHumanReview: NoMessageValues;
  docsMonitoring: NoMessageValues;
  docsCircuitBreaker: NoMessageValues;
  intro: NoMessageValues;
  features: NoMessageValues;
  feature1: NoMessageValues;
  feature2: NoMessageValues;
  feature3: NoMessageValues;
  feature4: NoMessageValues;
  feature5: NoMessageValues;
  feature6: NoMessageValues;
  feature7: NoMessageValues;
}>({
  title: {
    id: 'helpPanel.documents.title',
    defaultMessage: 'Documents',
    description: 'Heading of the help panel of the Document List page.',
  },
  learnMore: {
    id: 'helpPanel.documents.learnMore',
    defaultMessage: 'Learn more',
    description: 'Heading of the documentation links at the bottom of the Documents help panel.',
  },
  docsWebUi: {
    id: 'helpPanel.documents.docsWebUi',
    defaultMessage: 'Web UI',
    description: 'Documentation link in the Documents help panel: the page about the web interface.',
  },
  docsHumanReview: {
    id: 'helpPanel.documents.docsHumanReview',
    defaultMessage: 'Human Review',
    description: 'Documentation link in the Documents help panel: the page about human review of documents.',
  },
  docsMonitoring: {
    id: 'helpPanel.documents.docsMonitoring',
    defaultMessage: 'Monitoring',
    description: 'Documentation link in the Documents help panel: the page about monitoring.',
  },
  docsCircuitBreaker: {
    id: 'helpPanel.documents.docsCircuitBreaker',
    defaultMessage: 'Circuit Breaker',
    description: 'Documentation link in the Documents help panel: the page about the circuit breaker that pauses processing.',
  },
  intro: {
    id: 'helpPanel.documents.intro',
    defaultMessage: 'Track, search, filter, and manage all documents processed by the IDP system.',
    description: 'First sentence of the Documents help panel. "IDP" is the product abbreviation and may stay as it is in the translation.',
  },
  features: {
    id: 'helpPanel.documents.features',
    defaultMessage: 'Features',
    description: 'Heading above the list of what the Document List page offers, in its help panel.',
  },
  feature1: {
    id: 'helpPanel.documents.feature1',
    defaultMessage: 'View document processing status, classification, and confidence scores',
    description: 'First item of the feature list in the Documents help panel.',
  },
  feature2: {
    id: 'helpPanel.documents.feature2',
    defaultMessage: 'Filter by date range, status, document type, or any field using the search bar',
    description: 'Second item of the feature list in the Documents help panel.',
  },
  feature3: {
    id: 'helpPanel.documents.feature3',
    defaultMessage: 'Select documents to reprocess, delete, or abort active workflows',
    description: 'Third item of the feature list in the Documents help panel.',
  },
  feature4: {
    id: 'helpPanel.documents.feature4',
    defaultMessage: 'Claim or release documents for human review',
    description: 'Fourth item of the feature list in the Documents help panel.',
  },
  feature5: {
    id: 'helpPanel.documents.feature5',
    defaultMessage: 'Export filtered results to Excel for offline analysis',
    description: 'Fifth item of the feature list in the Documents help panel.',
  },
  feature6: {
    id: 'helpPanel.documents.feature6',
    defaultMessage: 'Drill down into individual documents for detailed inspection',
    description: 'Sixth item of the feature list in the Documents help panel.',
  },
  feature7: {
    id: 'helpPanel.documents.feature7',
    defaultMessage: 'Monitor the circuit breaker status and (admins only) pause, resume, or probe document processing',
    description: 'Seventh item of the feature list in the Documents help panel.',
  },
});

const ToolsPanel = (): React.JSX.Element => (
  <HelpPanel
    header={
      <h2>
        <FormattedMessage {...messages.title} />
      </h2>
    }
    footer={
      <div>
        <h3>
          <FormattedMessage {...messages.learnMore} /> <Icon name="external" />
        </h3>
        <ul>
          <li>
            <a href={`${DOCS_BASE_URL}/web-ui/`} target="_blank" rel="noopener noreferrer">
              <FormattedMessage {...messages.docsWebUi} />
            </a>
          </li>
          <li>
            <a href={`${DOCS_BASE_URL}/human-review/`} target="_blank" rel="noopener noreferrer">
              <FormattedMessage {...messages.docsHumanReview} />
            </a>
          </li>
          <li>
            <a href={`${DOCS_BASE_URL}/monitoring/`} target="_blank" rel="noopener noreferrer">
              <FormattedMessage {...messages.docsMonitoring} />
            </a>
          </li>
          <li>
            <a href={`${DOCS_BASE_URL}/circuit-breaker/`} target="_blank" rel="noopener noreferrer">
              <FormattedMessage {...messages.docsCircuitBreaker} />
            </a>
          </li>
        </ul>
      </div>
    }
  >
    <div>
      <p>
        <FormattedMessage {...messages.intro} />
      </p>
      <h3>
        <FormattedMessage {...messages.features} />
      </h3>
      <ul>
        <li>
          <FormattedMessage {...messages.feature1} />
        </li>
        <li>
          <FormattedMessage {...messages.feature2} />
        </li>
        <li>
          <FormattedMessage {...messages.feature3} />
        </li>
        <li>
          <FormattedMessage {...messages.feature4} />
        </li>
        <li>
          <FormattedMessage {...messages.feature5} />
        </li>
        <li>
          <FormattedMessage {...messages.feature6} />
        </li>
        <li>
          <FormattedMessage {...messages.feature7} />
        </li>
      </ul>
      <FeedbackHelpSection />
    </div>
  </HelpPanel>
);

export default ToolsPanel;
