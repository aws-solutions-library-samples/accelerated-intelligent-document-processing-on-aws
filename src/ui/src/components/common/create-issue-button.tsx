// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React from 'react';
import { ButtonDropdown } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, useIntl, type NoMessageValues } from 'react-intl';

import useDeploymentContext from '../../hooks/use-deployment-context';
import { buildBugReportUrl, buildFeatureRequestUrl } from '../../utils/github-feedback';

interface CreateIssueButtonProps {
  /**
   * Optional context text to attach (e.g. the latest agent message). Goes to
   * **Additional context** on both forms — not to the bug form's
   * Troubleshoot-agent field, whose heading would claim this was produced by
   * that agent about a document failure, which it need not be.
   */
  findings?: string;
  /**
   * Optional title suffix, e.g. a document key. Also surfaces as the document
   * name in Additional context. Neither current render site passes it.
   */
  titleHint?: string;
  variant?: 'normal' | 'icon' | 'inline-icon';
}

const messages = defineMessages<{ label: NoMessageValues; reportBug: NoMessageValues; requestFeature: NoMessageValues }>({
  label: {
    id: 'common.createIssueButton.label',
    defaultMessage: 'Create GitHub issue',
    description: 'Text and screen-reader label of the menu button under an Agent Chat answer that opens a pre-filled GitHub issue form.',
  },
  reportBug: {
    id: 'common.createIssueButton.reportBug',
    defaultMessage: 'Report a bug',
    description: 'Item of the Create GitHub issue menu under an Agent Chat answer: opens a pre-filled GitHub bug report in a new tab.',
  },
  requestFeature: {
    id: 'common.createIssueButton.requestFeature',
    defaultMessage: 'Request a feature',
    description: 'Item of the Create GitHub issue menu under an Agent Chat answer: opens a pre-filled GitHub feature request in a new tab.',
  },
});

/**
 * Small "Create GitHub issue" dropdown (Report a bug / Request a feature) that
 * pre-fills the GitHub issue forms with the current deployment's environment
 * details, and — for the bug path — any provided findings text. Opens the
 * pre-filled form in a new tab; nothing is submitted automatically.
 */
const CreateIssueButton = ({ findings, titleHint, variant = 'normal' }: CreateIssueButtonProps): React.JSX.Element => {
  const intl = useIntl();
  const deploymentContext = useDeploymentContext();

  // The chat answer goes to the bug form's "Additional context", not its
  // Troubleshoot-agent field: this button is also rendered from the Agent
  // Companion Chat, where the answer need not be about a document failure at
  // all, so that field's heading would mislabel it.
  const bugUrl = buildBugReportUrl(deploymentContext, titleHint ? { objectKey: titleHint } : undefined, findings);
  // Carry the same context (e.g. the chat answer) into the feature request so
  // "Request a feature" from chat isn't empty of context either.
  const featureUrl = buildFeatureRequestUrl(deploymentContext, findings);

  return (
    <ButtonDropdown
      variant={variant}
      expandableGroups={false}
      ariaLabel={intl.formatMessage(messages.label)}
      items={[
        { id: 'bug', text: intl.formatMessage(messages.reportBug), iconName: 'bug', href: bugUrl, external: true },
        { id: 'feature', text: intl.formatMessage(messages.requestFeature), iconName: 'suggestions', href: featureUrl, external: true },
      ]}
    >
      <FormattedMessage {...messages.label} />
    </ButtonDropdown>
  );
};

export default CreateIssueButton;
