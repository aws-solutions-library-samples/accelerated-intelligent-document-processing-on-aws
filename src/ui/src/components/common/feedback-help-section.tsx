// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React from 'react';
import { Icon } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, type MessageTag, type NoMessageValues } from 'react-intl';

import useDeploymentContext from '../../hooks/use-deployment-context';
import { buildBugReportUrl, buildFeatureRequestUrl } from '../../utils/github-feedback';
import { GITHUB_ISSUES_URL } from '../../constants/github';

const strong = (chunks: React.ReactNode[]): React.JSX.Element => <strong>{chunks}</strong>;

const messages = defineMessages<{
  title: NoMessageValues;
  intro: NoMessageValues;
  reportBug: NoMessageValues;
  requestFeature: NoMessageValues;
  viewIssues: NoMessageValues;
  troubleshootHint: { readonly strong: MessageTag };
}>({
  title: {
    id: 'helpPanel.feedback.title',
    defaultMessage: 'Feedback & support',
    description: 'Heading of the feedback block at the bottom of the Documents help panel, which lists the GitHub feedback links.',
  },
  intro: {
    id: 'helpPanel.feedback.intro',
    defaultMessage: 'Report bugs or suggest enhancements on GitHub — your deployment details are filled in automatically.',
    description: 'Sentence under the heading of the feedback block, above its three links.',
  },
  reportBug: {
    id: 'helpPanel.feedback.reportBug',
    defaultMessage: 'Report a bug',
    description: 'Link in the feedback block that opens a pre-filled GitHub bug report in a new tab.',
  },
  requestFeature: {
    id: 'helpPanel.feedback.requestFeature',
    defaultMessage: 'Request a feature',
    description: 'Link in the feedback block that opens a pre-filled GitHub feature request in a new tab.',
  },
  viewIssues: {
    id: 'helpPanel.feedback.viewIssues',
    defaultMessage: 'View existing issues',
    description: 'Link in the feedback block that opens the list of GitHub issues of the project in a new tab.',
  },
  troubleshootHint: {
    id: 'helpPanel.feedback.troubleshootHint',
    defaultMessage:
      'For document-processing failures, run the <strong>Troubleshoot</strong> agent on a failed document and use <strong>Report this issue on GitHub</strong> to attach its findings. Issues are public — please redact sensitive data before submitting.',
    description:
      'Note under the links of the feedback block. The two <strong> phrases are the names of controls rendered elsewhere, shown in bold: "Troubleshoot" is the action button on a document, "Report this issue on GitHub" the button in the dialog that shows its findings. Keep each consistent with the translation of that control.',
  },
});

/**
 * Reusable "Feedback & support" block for HelpPanel (right-side info) content.
 * Renders links that pre-fill the GitHub issue forms with the current
 * deployment's environment details. Embed inside any tools-panel's body.
 */
const FeedbackHelpSection = (): React.JSX.Element => {
  const deploymentContext = useDeploymentContext();
  const bugReportUrl = buildBugReportUrl(deploymentContext);
  const featureRequestUrl = buildFeatureRequestUrl(deploymentContext);

  return (
    <div>
      <h3>
        <FormattedMessage {...messages.title} />
      </h3>
      <p>
        <FormattedMessage {...messages.intro} />
      </p>
      <ul>
        <li>
          <a href={bugReportUrl} target="_blank" rel="noopener noreferrer">
            <FormattedMessage {...messages.reportBug} /> <Icon name="external" />
          </a>
        </li>
        <li>
          <a href={featureRequestUrl} target="_blank" rel="noopener noreferrer">
            <FormattedMessage {...messages.requestFeature} /> <Icon name="external" />
          </a>
        </li>
        <li>
          <a href={GITHUB_ISSUES_URL} target="_blank" rel="noopener noreferrer">
            <FormattedMessage {...messages.viewIssues} /> <Icon name="external" />
          </a>
        </li>
      </ul>
      <p>
        <FormattedMessage {...messages.troubleshootHint} values={{ strong }} />
      </p>
    </div>
  );
};

export default FeedbackHelpSection;
