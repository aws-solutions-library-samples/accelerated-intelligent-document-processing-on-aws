// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React, { useState } from 'react';
import { Alert, Box, ExpandableSection, SpaceBetween } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, useIntl, type NoMessageValues } from 'react-intl';

interface BedrockErrorInfo {
  errorType: string;
  message: string;
  technicalDetails?: string;
  actionRecommendations?: string[];
  retryAttempts?: number;
}

interface BedrockErrorMessageProps {
  errorInfo: BedrockErrorInfo;
  className?: string;
}

/** The card's title for each error type the agent reports; `default` covers any other type. */
const titleMessages = defineMessages<{
  serviceUnavailable: NoMessageValues;
  rateLimit: NoMessageValues;
  quotaExceeded: NoMessageValues;
  validationError: NoMessageValues;
  accessDenied: NoMessageValues;
  modelUnavailable: NoMessageValues;
  timeout: NoMessageValues;
  default: NoMessageValues;
}>({
  serviceUnavailable: {
    id: 'agentChat.bedrockError.title.serviceUnavailable',
    defaultMessage: 'Service Temporarily Unavailable',
    description: 'Title of the Agent Chat error card when the AI model service is temporarily unavailable.',
  },
  rateLimit: {
    id: 'agentChat.bedrockError.title.rateLimit',
    defaultMessage: 'Rate Limit Exceeded',
    description: 'Title of the Agent Chat error card when the AI model service refused the request because too many were sent.',
  },
  quotaExceeded: {
    id: 'agentChat.bedrockError.title.quotaExceeded',
    defaultMessage: 'Usage Quota Exceeded',
    description: "Title of the Agent Chat error card when the account's usage quota for the AI model service is used up.",
  },
  validationError: {
    id: 'agentChat.bedrockError.title.validationError',
    defaultMessage: 'Request Validation Error',
    description: 'Title of the Agent Chat error card when the AI model service rejected the request as invalid.',
  },
  accessDenied: {
    id: 'agentChat.bedrockError.title.accessDenied',
    defaultMessage: 'Access Denied',
    description: 'Title of the Agent Chat error card when the AI model service refused access.',
  },
  modelUnavailable: {
    id: 'agentChat.bedrockError.title.modelUnavailable',
    defaultMessage: 'AI Model Unavailable',
    description: 'Title of the Agent Chat error card when the AI model is not available.',
  },
  timeout: {
    id: 'agentChat.bedrockError.title.timeout',
    defaultMessage: 'Request Timeout',
    description: 'Title of the Agent Chat error card when the request to the AI model service timed out.',
  },
  default: {
    id: 'agentChat.bedrockError.title.default',
    defaultMessage: 'Service Error',
    description: 'Title of the Agent Chat error card for any other error from the AI model service.',
  },
});

const messages = defineMessages<{
  retries: { readonly count: number };
  whatYouCanDo: NoMessageValues;
  technicalDetails: NoMessageValues;
  errorType: { readonly type: string };
  details: { readonly details: string };
  retryAttempts: { readonly count: number };
}>({
  retries: {
    id: 'agentChat.bedrockError.retries',
    defaultMessage: '(After {count, plural, one {# retry attempt} other {# retry attempts}})',
    description: 'Shown beside the title of the Agent Chat error card. {count} is how many times the request was retried before the error.',
  },
  whatYouCanDo: {
    id: 'agentChat.bedrockError.whatYouCanDo',
    defaultMessage: 'What you can do:',
    description: 'Heading above the suggestions in the Agent Chat error card. The suggestions are shown as they arrive.',
  },
  technicalDetails: {
    id: 'agentChat.bedrockError.technicalDetails',
    defaultMessage: 'Technical Details',
    description: 'Header of the expandable section of the Agent Chat error card that shows the technical details.',
  },
  errorType: {
    id: 'agentChat.bedrockError.errorType',
    defaultMessage: 'Error Type: {type}',
    description: "Line in the error card's technical details. {type} is the error's code, such as rate_limit, as it arrives.",
  },
  details: {
    id: 'agentChat.bedrockError.details',
    defaultMessage: 'Details: {details}',
    description: "Line in the error card's technical details. {details} is the error text of the AI model service, as it arrives.",
  },
  retryAttempts: {
    id: 'agentChat.bedrockError.retryAttempts',
    defaultMessage: 'Retry Attempts: {count}',
    description: "Line in the error card's technical details. {count} is how many times the request was retried.",
  },
});

const BedrockErrorMessage = ({ errorInfo, className = '' }: BedrockErrorMessageProps): React.JSX.Element => {
  const intl = useIntl();
  const [showTechnicalDetails, setShowTechnicalDetails] = useState(false);

  // Map error types to appropriate alert types and status indicators
  const getErrorDisplayInfo = (errorType: string) => {
    switch (errorType) {
      case 'service_unavailable':
        return {
          alertType: 'warning' as const,
          statusType: 'warning',
          icon: 'warning',
          title: titleMessages.serviceUnavailable,
        };
      case 'rate_limit':
      case 'model_throttling':
      case 'too_many_requests':
        return {
          alertType: 'warning' as const,
          statusType: 'warning',
          icon: 'warning',
          title: titleMessages.rateLimit,
        };
      case 'quota_exceeded':
        return {
          alertType: 'error' as const,
          statusType: 'error',
          icon: 'error',
          title: titleMessages.quotaExceeded,
        };
      case 'validation_error':
        return {
          alertType: 'error' as const,
          statusType: 'error',
          icon: 'error',
          title: titleMessages.validationError,
        };
      case 'access_denied':
        return {
          alertType: 'error' as const,
          statusType: 'error',
          icon: 'error',
          title: titleMessages.accessDenied,
        };
      case 'model_unavailable':
        return {
          alertType: 'warning' as const,
          statusType: 'warning',
          icon: 'warning',
          title: titleMessages.modelUnavailable,
        };
      case 'timeout':
        return {
          alertType: 'warning' as const,
          statusType: 'warning',
          icon: 'warning',
          title: titleMessages.timeout,
        };
      default:
        return {
          alertType: 'error' as const,
          statusType: 'error',
          icon: 'error',
          title: titleMessages.default,
        };
    }
  };

  const displayInfo = getErrorDisplayInfo(errorInfo.errorType);

  return (
    <div className={`bedrock-error-message ${className}`}>
      <Alert
        type={displayInfo.alertType}
        header={
          <SpaceBetween direction="horizontal" size="xs" alignItems="center">
            <span>
              <FormattedMessage {...displayInfo.title} />
            </span>
            {(errorInfo.retryAttempts ?? 0) > 0 && (
              <Box fontSize="body-s" color="text-status-inactive">
                <FormattedMessage {...messages.retries} values={{ count: errorInfo.retryAttempts ?? 0 }} />
              </Box>
            )}
          </SpaceBetween>
        }
      >
        <SpaceBetween size="m">
          {/* Main error message */}
          <Box>{errorInfo.message}</Box>

          {/* Action recommendations */}
          {errorInfo.actionRecommendations && errorInfo.actionRecommendations.length > 0 && (
            <Box>
              <Box fontWeight="bold" fontSize="body-s" color="text-label">
                <FormattedMessage {...messages.whatYouCanDo} />
              </Box>
              <ul style={{ margin: '8px 0', paddingLeft: '20px' }}>
                {errorInfo.actionRecommendations.map((recommendation) => (
                  <li key={recommendation} style={{ marginBottom: '4px' }}>
                    <Box fontSize="body-s">{recommendation}</Box>
                  </li>
                ))}
              </ul>
            </Box>
          )}

          {/* Technical details (expandable) */}
          {errorInfo.technicalDetails && (
            <ExpandableSection
              headerText={intl.formatMessage(messages.technicalDetails)}
              variant="footer"
              expanded={showTechnicalDetails}
              onChange={({ detail }) => setShowTechnicalDetails(detail.expanded)}
            >
              <Box
                padding="s"
                fontSize="body-s"
                {...({ backgroundColor: 'background-container-content', fontFamily: 'monospace' } as Record<string, unknown>)}
              >
                <Box color="text-body-secondary">
                  <FormattedMessage {...messages.errorType} values={{ type: errorInfo.errorType }} />
                </Box>
                <Box color="text-body-secondary" margin={{ top: 'xs' }}>
                  <FormattedMessage {...messages.details} values={{ details: errorInfo.technicalDetails }} />
                </Box>
                {(errorInfo.retryAttempts ?? 0) > 0 && (
                  <Box color="text-body-secondary" margin={{ top: 'xs' }}>
                    <FormattedMessage {...messages.retryAttempts} values={{ count: errorInfo.retryAttempts ?? 0 }} />
                  </Box>
                )}
              </Box>
            </ExpandableSection>
          )}
        </SpaceBetween>
      </Alert>
    </div>
  );
};

export default BedrockErrorMessage;
