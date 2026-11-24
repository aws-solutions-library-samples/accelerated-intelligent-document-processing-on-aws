// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React from 'react';
import { HelpPanel, Icon } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, type MessageTag, type NoMessageValues } from 'react-intl';

const DOCS_BASE_URL = 'https://aws-solutions-library-samples.github.io/accelerated-intelligent-document-processing-on-aws';

type Bold = { readonly b: MessageTag };

/** Renders a `<b>...</b>` element value: a control of the action bar named inside a sentence. */
const bold = (chunks: React.ReactNode[]): React.JSX.Element => <b>{chunks}</b>;

const messages = defineMessages<{
  title: NoMessageValues;
  learnMore: NoMessageValues;
  docsWebUi: NoMessageValues;
  docsClassification: NoMessageValues;
  docsExtraction: NoMessageValues;
  docsAssessment: NoMessageValues;
  docsHumanReview: NoMessageValues;
  intro: NoMessageValues;
  actionBar: NoMessageValues;
  barSwitch: Bold;
  barReports: Bold;
  barMoreActions: Bold;
  barButtons: Bold;
  unavailableActions: NoMessageValues;
  onThePage: NoMessageValues;
  pageFields: NoMessageValues;
  pageSections: NoMessageValues;
  pagePages: NoMessageValues;
  pageColumns: NoMessageValues;
  pageVersions: NoMessageValues;
}>({
  title: {
    id: 'helpPanel.documentDetails.title',
    defaultMessage: 'Document Details',
    description: 'Heading of the help panel of a document page.',
  },
  learnMore: {
    id: 'helpPanel.documentDetails.learnMore',
    defaultMessage: 'Learn more',
    description: 'Heading of the documentation links at the bottom of the document page help panel.',
  },
  docsWebUi: {
    id: 'helpPanel.documentDetails.docsWebUi',
    defaultMessage: 'Web UI',
    description: 'Documentation link in the document page help panel: the page about the web interface.',
  },
  docsClassification: {
    id: 'helpPanel.documentDetails.docsClassification',
    defaultMessage: 'Classification',
    description: 'Documentation link in the document page help panel: the page about document classification.',
  },
  docsExtraction: {
    id: 'helpPanel.documentDetails.docsExtraction',
    defaultMessage: 'Extraction',
    description: 'Documentation link in the document page help panel: the page about field extraction.',
  },
  docsAssessment: {
    id: 'helpPanel.documentDetails.docsAssessment',
    defaultMessage: 'Assessment',
    description: 'Documentation link in the document page help panel: the page about confidence assessment.',
  },
  docsHumanReview: {
    id: 'helpPanel.documentDetails.docsHumanReview',
    defaultMessage: 'Human Review',
    description: 'Documentation link in the document page help panel: the page about human review of documents.',
  },
  intro: {
    id: 'helpPanel.documentDetails.intro',
    defaultMessage: "Inspect a document's processing results, and act on it from one bar at the top of the page.",
    description: 'First sentence of the document page help panel.',
  },
  actionBar: {
    id: 'helpPanel.documentDetails.actionBar',
    defaultMessage: 'The action bar',
    description: 'Sub-heading above the bullets about the bar of buttons at the top of the document page.',
  },
  barSwitch: {
    id: 'helpPanel.documentDetails.barSwitch',
    defaultMessage: "<b>Details | Source</b> switches between the page's panels and the original file.",
    description: 'Help panel bullet about the action bar; the bold text names the two-way switch as the bar labels it.',
  },
  barReports: {
    id: 'helpPanel.documentDetails.barReports',
    defaultMessage:
      '<b>Reports</b>: the Summary, Evaluation Report, Rule Validation Summary, Estimated Cost and Processing Flow, each in a dialog.',
    description: 'Help panel bullet about the action bar; the bold text and the report names match the Reports menu.',
  },
  barMoreActions: {
    id: 'helpPanel.documentDetails.barMoreActions',
    defaultMessage:
      "<b>More Actions</b>: Edit Page Grouping, Re-extract Sections, Edit Page Text, Reset Page Class, Use As Evaluation Baseline, Skip All Reviews; then the downloads (the document's outputs as a ZIP); then Reprocess, Abort and Delete.",
    description: 'Help panel bullet about the action bar; the bold text and the action names match the More Actions menu.',
  },
  barButtons: {
    id: 'helpPanel.documentDetails.barButtons',
    defaultMessage: "<b>Refresh</b> the page, <b>Troubleshoot</b> what went wrong, and <b>Edit</b> the document's extracted values.",
    description: 'Help panel bullet about the action bar; each bold word is a button label on the bar.',
  },
  unavailableActions: {
    id: 'helpPanel.documentDetails.unavailableActions',
    defaultMessage:
      "Actions your role can't perform are not shown. One that doesn't apply now, for example while you view a past version, stays in place, disabled; hover it to see why.",
    description: 'Paragraph under the action bar bullets: why an action can be missing or disabled.',
  },
  onThePage: {
    id: 'helpPanel.documentDetails.onThePage',
    defaultMessage: 'On the page',
    description: 'Sub-heading above the bullets about the panels of the document page.',
  },
  pageFields: {
    id: 'helpPanel.documentDetails.pageFields',
    defaultMessage: "The document's fields and status",
    description: 'Help panel bullet about the Document Details panel of the page.',
  },
  pageSections: {
    id: 'helpPanel.documentDetails.pageSections',
    defaultMessage: "Each section's class, pages and status; its ⋮ menu opens or downloads its data",
    description: 'Help panel bullet about the sections table; ⋮ is the row menu icon and stays as it is.',
  },
  pagePages: {
    id: 'helpPanel.documentDetails.pagePages',
    defaultMessage: "Each page's class, confidence and thumbnail; its ⋮ menu opens the page text",
    description: 'Help panel bullet about the pages table; ⋮ is the row menu icon and stays as it is.',
  },
  pageColumns: {
    id: 'helpPanel.documentDetails.pageColumns',
    defaultMessage: 'The ⚙ icon on each table chooses which columns it shows',
    description: 'Help panel bullet about the column preferences; ⚙ is the settings icon and stays as it is.',
  },
  pageVersions: {
    id: 'helpPanel.documentDetails.pageVersions',
    defaultMessage: "Version History, to view (from each row's ⋮ menu) or compare past processing runs, and Chat with the document",
    description: 'Help panel bullet about the Version History and Chat panels; ⋮ is the row menu icon and stays as it is.',
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
            <a href={`${DOCS_BASE_URL}/classification/`} target="_blank" rel="noopener noreferrer">
              <FormattedMessage {...messages.docsClassification} />
            </a>
          </li>
          <li>
            <a href={`${DOCS_BASE_URL}/extraction/`} target="_blank" rel="noopener noreferrer">
              <FormattedMessage {...messages.docsExtraction} />
            </a>
          </li>
          <li>
            <a href={`${DOCS_BASE_URL}/assessment/`} target="_blank" rel="noopener noreferrer">
              <FormattedMessage {...messages.docsAssessment} />
            </a>
          </li>
          <li>
            <a href={`${DOCS_BASE_URL}/human-review/`} target="_blank" rel="noopener noreferrer">
              <FormattedMessage {...messages.docsHumanReview} />
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
        <FormattedMessage {...messages.actionBar} />
      </h3>
      <ul>
        <li>
          <FormattedMessage {...messages.barSwitch} values={{ b: bold }} />
        </li>
        <li>
          <FormattedMessage {...messages.barReports} values={{ b: bold }} />
        </li>
        <li>
          <FormattedMessage {...messages.barMoreActions} values={{ b: bold }} />
        </li>
        <li>
          <FormattedMessage {...messages.barButtons} values={{ b: bold }} />
        </li>
      </ul>
      <p>
        <FormattedMessage {...messages.unavailableActions} />
      </p>
      <h3>
        <FormattedMessage {...messages.onThePage} />
      </h3>
      <ul>
        <li>
          <FormattedMessage {...messages.pageFields} />
        </li>
        <li>
          <FormattedMessage {...messages.pageSections} />
        </li>
        <li>
          <FormattedMessage {...messages.pagePages} />
        </li>
        <li>
          <FormattedMessage {...messages.pageColumns} />
        </li>
        <li>
          <FormattedMessage {...messages.pageVersions} />
        </li>
      </ul>
    </div>
  </HelpPanel>
);

export default ToolsPanel;
