// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React from 'react';
import { ButtonDropdown } from '@cloudscape-design/components';

import useDeploymentContext from '../../hooks/use-deployment-context';
import { buildBugReportUrl, buildFeatureRequestUrl } from '../../utils/github-feedback';

interface CreateIssueButtonProps {
  /**
   * Optional findings/context text to attach to a bug report (e.g. an agent
   * message). Included in the pre-filled bug form's troubleshoot field.
   */
  findings?: string;
  /** Optional title suffix, e.g. a document key. */
  titleHint?: string;
  variant?: 'normal' | 'icon' | 'inline-icon';
}

/**
 * Small "Create GitHub issue" dropdown (Report a bug / Request a feature) that
 * pre-fills the GitHub issue forms with the current deployment's environment
 * details, and — for the bug path — any provided findings text. Opens the
 * pre-filled form in a new tab; nothing is submitted automatically.
 */
const CreateIssueButton = ({ findings, titleHint, variant = 'normal' }: CreateIssueButtonProps): React.JSX.Element => {
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
      ariaLabel="Create GitHub issue"
      items={[
        { id: 'bug', text: 'Report a bug', iconName: 'bug', href: bugUrl, external: true },
        { id: 'feature', text: 'Request a feature', iconName: 'suggestions', href: featureUrl, external: true },
      ]}
    >
      Create GitHub issue
    </ButtonDropdown>
  );
};

export default CreateIssueButton;
