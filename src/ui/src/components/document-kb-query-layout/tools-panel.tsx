// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React from 'react';
import { HelpPanel, Icon } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, type NoMessageValues } from 'react-intl';

const DOCS_BASE_URL = 'https://aws-solutions-library-samples.github.io/accelerated-intelligent-document-processing-on-aws';

const messages = defineMessages<{
  title: NoMessageValues;
  learnMore: NoMessageValues;
  docsKnowledgeBase: NoMessageValues;
  intro: NoMessageValues;
  features: NoMessageValues;
  feature1: NoMessageValues;
  feature2: NoMessageValues;
  feature3: NoMessageValues;
  tips: NoMessageValues;
  tip1: NoMessageValues;
  tip2: NoMessageValues;
  tip3: NoMessageValues;
}>({
  title: {
    id: 'helpPanel.knowledgeBase.title',
    defaultMessage: 'Document Knowledge Base Query',
    description: 'Heading of the help panel of the Knowledge Base page.',
  },
  learnMore: {
    id: 'helpPanel.knowledgeBase.learnMore',
    defaultMessage: 'Learn more',
    description: 'Heading of the documentation links at the bottom of the Knowledge Base help panel.',
  },
  docsKnowledgeBase: {
    id: 'helpPanel.knowledgeBase.docsKnowledgeBase',
    defaultMessage: 'Knowledge Base',
    description: 'Documentation link in the Knowledge Base help panel: the page about the document knowledge base.',
  },
  intro: {
    id: 'helpPanel.knowledgeBase.intro',
    defaultMessage: 'Query your document collection using natural language, powered by Amazon Bedrock Knowledge Bases.',
    description:
      'First paragraph of the Knowledge Base help panel. "Amazon Bedrock Knowledge Bases" is an AWS service name and stays as it is.',
  },
  features: {
    id: 'helpPanel.knowledgeBase.features',
    defaultMessage: 'Features',
    description: 'Heading above the list of features in the Knowledge Base help panel.',
  },
  feature1: {
    id: 'helpPanel.knowledgeBase.feature1',
    defaultMessage: 'Ask questions about documents in your knowledge base',
    description: 'First item of the features list in the Knowledge Base help panel.',
  },
  feature2: {
    id: 'helpPanel.knowledgeBase.feature2',
    defaultMessage: 'Get responses with citations to source documents',
    description: 'Second item of the features list in the Knowledge Base help panel.',
  },
  feature3: {
    id: 'helpPanel.knowledgeBase.feature3',
    defaultMessage: 'Follow document links to view original source material',
    description: 'Third item of the features list in the Knowledge Base help panel.',
  },
  tips: {
    id: 'helpPanel.knowledgeBase.tips',
    defaultMessage: 'Tips',
    description: 'Heading above the list of tips in the Knowledge Base help panel.',
  },
  tip1: {
    id: 'helpPanel.knowledgeBase.tip1',
    defaultMessage: 'Be specific in your questions to get more accurate answers',
    description: 'First item of the tips list in the Knowledge Base help panel.',
  },
  tip2: {
    id: 'helpPanel.knowledgeBase.tip2',
    defaultMessage: 'Questions are context-aware — you can ask follow-up questions',
    description: 'Second item of the tips list in the Knowledge Base help panel.',
  },
  tip3: {
    id: 'helpPanel.knowledgeBase.tip3',
    defaultMessage: 'Results are based on indexed document content',
    description: 'Third item of the tips list in the Knowledge Base help panel.',
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
            <a href={`${DOCS_BASE_URL}/knowledge-base/`} target="_blank" rel="noopener noreferrer">
              <FormattedMessage {...messages.docsKnowledgeBase} />
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
      </ul>
      <h3>
        <FormattedMessage {...messages.tips} />
      </h3>
      <ul>
        <li>
          <FormattedMessage {...messages.tip1} />
        </li>
        <li>
          <FormattedMessage {...messages.tip2} />
        </li>
        <li>
          <FormattedMessage {...messages.tip3} />
        </li>
      </ul>
    </div>
  </HelpPanel>
);

export default ToolsPanel;
