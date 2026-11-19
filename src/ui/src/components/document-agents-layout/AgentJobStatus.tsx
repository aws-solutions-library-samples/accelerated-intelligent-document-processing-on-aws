// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import React from 'react';
import { StatusIndicator, Box, SpaceBetween } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, type NoMessageValues } from 'react-intl';

const messages = defineMessages<{
  pending: NoMessageValues;
  processing: NoMessageValues;
  completed: NoMessageValues;
  failed: NoMessageValues;
  errorLabel: NoMessageValues;
}>({
  pending: {
    id: 'agents.jobStatus.pending',
    defaultMessage: 'Job created, waiting to start processing',
    description: "Status of the agents' job on the agents page: the job exists and has not started yet.",
  },
  processing: {
    id: 'agents.jobStatus.processing',
    defaultMessage: 'Processing your query',
    description: "Status of the agents' job on the agents page: the agents are working on the question.",
  },
  completed: {
    id: 'agents.jobStatus.completed',
    defaultMessage: 'Processing complete',
    description: "Status of the agents' job on the agents page: the agents have answered.",
  },
  failed: {
    id: 'agents.jobStatus.failed',
    defaultMessage: 'Processing failed',
    description: "Status of the agents' job on the agents page: the job failed.",
  },
  errorLabel: {
    id: 'agents.jobStatus.errorLabel',
    defaultMessage: 'Error:',
    description: 'Bold label before an error on the agents page. The error text after it is shown as it arrives.',
  },
});

const getStatusIndicator = (status: string | null): React.JSX.Element | null => {
  switch (status) {
    case 'PENDING':
      return (
        <StatusIndicator type="pending">
          <FormattedMessage {...messages.pending} />
        </StatusIndicator>
      );
    case 'PROCESSING':
      return (
        <StatusIndicator type="in-progress">
          <FormattedMessage {...messages.processing} />
        </StatusIndicator>
      );
    case 'COMPLETED':
      return (
        <StatusIndicator type="success">
          <FormattedMessage {...messages.completed} />
        </StatusIndicator>
      );
    case 'FAILED':
      return (
        <StatusIndicator type="error">
          <FormattedMessage {...messages.failed} />
        </StatusIndicator>
      );
    default:
      return null;
  }
};

interface AgentJobStatusProps {
  jobId?: string | null;
  status?: string | null;
  error?: string | null;
}

const AgentJobStatus = ({ jobId = null, status = null, error = null }: AgentJobStatusProps): React.JSX.Element | null => {
  // Show error even if there's no jobId (for validation errors)
  if (error && !jobId) {
    return (
      <Box padding={{ vertical: 'xs' }}>
        <div>
          <strong>
            <FormattedMessage {...messages.errorLabel} />
          </strong>{' '}
          {error}
        </div>
      </Box>
    );
  }

  if (!jobId) {
    return null;
  }

  return (
    <Box padding={{ vertical: 'xs' }}>
      <SpaceBetween direction="vertical" size="xs">
        <div>{getStatusIndicator(status)}</div>
        {error && (
          <div>
            <strong>
              <FormattedMessage {...messages.errorLabel} />
            </strong>{' '}
            {error}
          </div>
        )}
      </SpaceBetween>
    </Box>
  );
};

export default AgentJobStatus;
