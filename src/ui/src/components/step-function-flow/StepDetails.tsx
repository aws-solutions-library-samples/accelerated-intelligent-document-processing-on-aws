// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React, { useState } from 'react';
import { defineMessages, FormattedMessage, useIntl, type MessageTag, type NoMessageValues } from 'react-intl';
import { Box, SpaceBetween, ExpandableSection, Button, Alert, Container } from '@cloudscape-design/components';
import './StepDetails.css';
import { parseStepFunctionPayload } from '../../graphql/awsjson-parsers';
import { useFormatters } from '../../i18n/format';
import type { MalwareScanFields } from '../common/malware-scan';
import { stepLabelMessages, stepName, stepOutput, stepStatusLabel } from './step-labels';
import { isStepDisabled, type StepConfig } from './is-step-disabled';
import SkippedStatus from './SkippedStatus';
import { SKIPPED_STATUS } from './skipped-steps';

interface Step {
  name: string;
  type: string;
  status: string;
  startDate?: string;
  stopDate?: string;
  input?: string;
  output?: string;
  error?: string;
  mapIterations?: number;
  mapIterationDetails?: Step[];
  /** Set on the entry of a step the run plan skipped: why it was not done. */
  skipNote?: string;
  /** Set on the Malware Scan step only: the document's verdict, which the step is recognised and worded by. */
  malwareScan?: MalwareScanFields;
}

interface JsonDisplayProps {
  data?: string | Record<string, unknown> | null;
}

interface StepDetailsProps {
  step: Step;
  formatDuration: (startDate?: string, stopDate?: string) => string;
  getStepIcon: (name: string, type: string, status: string) => React.ReactNode;
  mergedConfig?: StepConfig | null;
}

const messages = defineMessages<{
  type: { readonly type: string };
  disabledHeader: NoMessageValues;
  disabledNotice: { readonly setting: string; readonly strong: MessageTag };
  stepError: NoMessageValues;
  errorDetails: NoMessageValues;
  inputData: NoMessageValues;
  copyInput: NoMessageValues;
  noInput: NoMessageValues;
  mapIterations: { readonly count: number };
  iterationError: NoMessageValues;
  outputData: NoMessageValues;
  copyOutput: NoMessageValues;
  noOutput: NoMessageValues;
}>({
  type: {
    id: 'documents.processingFlow.step.type',
    defaultMessage: 'Type: {type}',
    description:
      "Under the chosen step's name in the Step Details of Reports › Processing Flow on the document page. {type} is the step's state type as the execution reports it (Task, Choice, Map …), in English.",
  },
  disabledHeader: {
    id: 'documents.processingFlow.step.disabledHeader',
    defaultMessage: 'Step Disabled in Configuration',
    description:
      'Header of the notice in the Step Details of Reports › Processing Flow on the document page when the configuration the document was processed with turned the chosen step off.',
  },
  disabledNotice: {
    id: 'documents.processingFlow.step.disabledNotice',
    defaultMessage:
      'This step was disabled in the configuration (<strong>{setting}</strong>) and performed no processing. While the step function executed this step, the Lambda function detected the disabled state and skipped all processing logic.',
    description:
      'Notice in the Step Details of Reports › Processing Flow on the document page when the configuration the document was processed with turned the chosen step off. {setting} is the configuration setting that turns it off, enabled: false, shown as written; <strong> makes it bold.',
  },
  stepError: {
    id: 'documents.processingFlow.step.stepError',
    defaultMessage: 'Step Error',
    description:
      'Header of the expandable section in the Step Details of Reports › Processing Flow on the document page that shows the error the chosen step failed with.',
  },
  errorDetails: {
    id: 'documents.processingFlow.step.errorDetails',
    defaultMessage: 'Error Details',
    description:
      'Header of the alert inside the Step Error section in the Step Details of Reports › Processing Flow on the document page. The alert shows the error the step reported, in English.',
  },
  inputData: {
    id: 'documents.processingFlow.step.inputData',
    defaultMessage: 'Input Data',
    description:
      'Header of the expandable section in the Step Details of Reports › Processing Flow on the document page that shows the data the chosen step received.',
  },
  copyInput: {
    id: 'documents.processingFlow.step.copyInput',
    defaultMessage: 'Copy input data',
    description:
      'Screen-reader label of the copy button of the Input Data section in the Step Details of Reports › Processing Flow on the document page.',
  },
  noInput: {
    id: 'documents.processingFlow.step.noInput',
    defaultMessage: 'No input data available for this step.',
    description: 'Shown in the Step Details of Reports › Processing Flow on the document page when the chosen step has no input data.',
  },
  mapIterations: {
    id: 'documents.processingFlow.step.mapIterations',
    defaultMessage: 'Map Iterations ({count})',
    description:
      'Header of the expandable section in the Step Details of Reports › Processing Flow on the document page that lists the iterations of the chosen Map step. {count} is the number of iterations. Map is a Step Functions state type.',
  },
  iterationError: {
    id: 'documents.processingFlow.step.iterationError',
    defaultMessage: 'Iteration Error',
    description:
      'Header of the alert under a failed iteration of the chosen Map step in the Step Details of Reports › Processing Flow on the document page. The alert shows the error the iteration reported, in English.',
  },
  outputData: {
    id: 'documents.processingFlow.step.outputData',
    defaultMessage: 'Output Data',
    description:
      'Header of the expandable section in the Step Details of Reports › Processing Flow on the document page that shows what the chosen step returned; for the Malware Scan step, the explanation of its verdict.',
  },
  copyOutput: {
    id: 'documents.processingFlow.step.copyOutput',
    defaultMessage: 'Copy output data',
    description:
      'Screen-reader label of the copy button of the Output Data section in the Step Details of Reports › Processing Flow on the document page.',
  },
  noOutput: {
    id: 'documents.processingFlow.step.noOutput',
    defaultMessage: 'No output data available for this step.',
    description: 'Shown in the Step Details of Reports › Processing Flow on the document page when the chosen step has no output data.',
  },
});

const strong = (chunks: React.ReactNode[]): React.JSX.Element => <strong>{chunks}</strong>;

const JsonDisplay = ({ data = null }: JsonDisplayProps): React.JSX.Element | null => {
  if (!data) return null;

  const formatJson = (jsonString: string | Record<string, unknown>): string => {
    // Handle different data types
    if (typeof jsonString === 'object') {
      try {
        return JSON.stringify(jsonString, null, 2);
      } catch {
        return String(jsonString);
      }
    }

    if (typeof jsonString === 'string') {
      const parsed = parseStepFunctionPayload(jsonString);
      if (parsed) {
        return JSON.stringify(parsed, null, 2);
      }
      // If not JSON, return as-is
      return jsonString;
    }

    return String(jsonString);
  };

  const formattedData = formatJson(data);

  return (
    <Container>
      <Box>
        <pre className="json-display">{formattedData}</pre>
      </Box>
    </Container>
  );
};

const StepDetails = ({ step, formatDuration, getStepIcon, mergedConfig = null }: StepDetailsProps): React.JSX.Element => {
  const intl = useIntl();
  const { formatDateTime } = useFormatters();
  const [inputExpanded, setInputExpanded] = useState(false);
  const [outputExpanded, setOutputExpanded] = useState(false);
  const [errorExpanded, setErrorExpanded] = useState(true); // Default to expanded for errors

  const stepSkipped = step.status === SKIPPED_STATUS;
  const stepDisabled = !stepSkipped && isStepDisabled(step.name, mergedConfig);
  // What Output Data shows, and so what its copy button copies.
  const output = stepOutput(step, intl);

  const formatJson = (jsonString: string | undefined): string => {
    if (!jsonString) return '';
    const parsed = parseStepFunctionPayload(jsonString);
    if (parsed) {
      return JSON.stringify(parsed, null, 2);
    }
    return jsonString;
  };

  const copyToClipboard = (text: string): void => {
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text);
      } else {
        // Fallback for older browsers
        const textArea = document.createElement('textarea');
        textArea.value = text;
        document.body.appendChild(textArea);
        textArea.focus();
        textArea.select();
        document.execCommand('copy');
        document.body.removeChild(textArea);
      }
    } catch (error) {
      console.warn('Failed to copy to clipboard:', error);
    }
  };

  // A step the run plan skipped was never entered: it has a name and a reason, and no
  // times, input, output or error to show.
  if (stepSkipped) {
    return (
      <div className="step-details">
        <SpaceBetween size="l">
          <Box>
            <SpaceBetween direction="horizontal" size="m" alignItems="center">
              <div className="step-details-icon">{getStepIcon(step.name, step.type, step.status)}</div>
              <div>
                <Box variant="h3">{stepName(step, intl)}</Box>
                <SkippedStatus />
              </div>
            </SpaceBetween>
          </Box>
          {step.skipNote && <Alert type="info">{step.skipNote}</Alert>}
        </SpaceBetween>
      </div>
    );
  }

  return (
    <div className="step-details">
      <SpaceBetween size="l">
        {/* Step Header */}
        <Box>
          <SpaceBetween direction="horizontal" size="m" alignItems="center">
            <div className="step-details-icon">{getStepIcon(step.name, step.type, step.status)}</div>
            <div>
              <Box variant="h3">{stepName(step, intl)}</Box>
              <Box variant="small" color="text-status-inactive">
                <FormattedMessage {...messages.type} values={{ type: step.type }} />
              </Box>
            </div>
          </SpaceBetween>
        </Box>

        {/* Configuration Disabled Notice */}
        {stepDisabled && (
          <Alert type="info" header={intl.formatMessage(messages.disabledHeader)}>
            <FormattedMessage {...messages.disabledNotice} values={{ setting: 'enabled: false', strong }} />
          </Alert>
        )}

        {/* Step Metadata */}
        <div className="step-metadata">
          <SpaceBetween direction="horizontal" size="l">
            <Box>
              <Box variant="awsui-key-label">
                <FormattedMessage {...stepLabelMessages.status} />
              </Box>
              <Box className={`step-status step-status-${step.status.toLowerCase()}`}>{stepStatusLabel(step.status, intl)}</Box>
            </Box>
            <Box>
              <Box variant="awsui-key-label">
                <FormattedMessage {...stepLabelMessages.duration} />
              </Box>
              <Box>{formatDuration(step.startDate, step.stopDate)}</Box>
            </Box>
            <Box>
              <Box variant="awsui-key-label">
                <FormattedMessage {...stepLabelMessages.started} />
              </Box>
              <Box>
                {step.startDate ? formatDateTime(new Date(step.startDate)) : <FormattedMessage {...stepLabelMessages.notAvailable} />}
              </Box>
            </Box>
            {step.stopDate && (
              <Box>
                <Box variant="awsui-key-label">
                  <FormattedMessage {...stepLabelMessages.completed} />
                </Box>
                <Box>{formatDateTime(new Date(step.stopDate))}</Box>
              </Box>
            )}
          </SpaceBetween>
        </div>

        {/* Error Information */}
        {step.error && (
          <ExpandableSection
            headerText={intl.formatMessage(messages.stepError)}
            {...({ variant: 'error' } as Record<string, unknown>)}
            expanded={errorExpanded}
            onChange={({ detail }) => setErrorExpanded(detail.expanded)}
          >
            <Alert type="error" header={intl.formatMessage(messages.errorDetails)}>
              <Box>
                <pre className="error-message">{step.error}</pre>
              </Box>
            </Alert>
          </ExpandableSection>
        )}

        {/* Input Data */}
        {step.input ? (
          <ExpandableSection
            headerText={intl.formatMessage(messages.inputData)}
            expanded={inputExpanded}
            onChange={({ detail }) => setInputExpanded(detail.expanded)}
            headerActions={
              <Button
                variant="inline-icon"
                iconName="copy"
                onClick={() => copyToClipboard(formatJson(step.input))}
                ariaLabel={intl.formatMessage(messages.copyInput)}
              />
            }
          >
            <JsonDisplay data={step.input} />
          </ExpandableSection>
        ) : (
          <Box variant="p" color="text-status-inactive">
            <FormattedMessage {...messages.noInput} />
          </Box>
        )}

        {/* Map Iterations */}
        {step.type === 'Map' && step.mapIterationDetails && step.mapIterationDetails.length > 0 && (
          <ExpandableSection
            headerText={intl.formatMessage(messages.mapIterations, { count: step.mapIterationDetails.length })}
            expanded={false}
          >
            <SpaceBetween size="m">
              {step.mapIterationDetails.map((iteration, index) => (
                <Container key={`iteration-${iteration.name}-${iteration.startDate || index}`} header={stepName(iteration, intl)}>
                  <SpaceBetween size="s">
                    <div className="iteration-metadata">
                      <SpaceBetween direction="horizontal" size="m">
                        <Box>
                          <Box variant="awsui-key-label">
                            <FormattedMessage {...stepLabelMessages.status} />
                          </Box>
                          <Box className={`step-status step-status-${iteration.status.toLowerCase()}`}>
                            {stepStatusLabel(iteration.status, intl)}
                          </Box>
                        </Box>
                        <Box>
                          <Box variant="awsui-key-label">
                            <FormattedMessage {...stepLabelMessages.duration} />
                          </Box>
                          <Box>{formatDuration(iteration.startDate, iteration.stopDate)}</Box>
                        </Box>
                        <Box>
                          <Box variant="awsui-key-label">
                            <FormattedMessage {...stepLabelMessages.started} />
                          </Box>
                          <Box>
                            {iteration.startDate ? (
                              formatDateTime(new Date(iteration.startDate))
                            ) : (
                              <FormattedMessage {...stepLabelMessages.notAvailable} />
                            )}
                          </Box>
                        </Box>
                        {iteration.stopDate && (
                          <Box>
                            <Box variant="awsui-key-label">
                              <FormattedMessage {...stepLabelMessages.completed} />
                            </Box>
                            <Box>{formatDateTime(new Date(iteration.stopDate))}</Box>
                          </Box>
                        )}
                      </SpaceBetween>
                    </div>
                    {iteration.error && (
                      <Alert type="error" header={intl.formatMessage(messages.iterationError)}>
                        <pre className="error-message">{iteration.error}</pre>
                      </Alert>
                    )}
                  </SpaceBetween>
                </Container>
              ))}
            </SpaceBetween>
          </ExpandableSection>
        )}

        {/* Output Data */}
        {output ? (
          <ExpandableSection
            headerText={intl.formatMessage(messages.outputData)}
            expanded={outputExpanded}
            onChange={({ detail }) => setOutputExpanded(detail.expanded)}
            headerActions={
              <Button
                variant="inline-icon"
                iconName="copy"
                onClick={() => copyToClipboard(formatJson(output))}
                ariaLabel={intl.formatMessage(messages.copyOutput)}
              />
            }
          >
            <JsonDisplay data={output} />
          </ExpandableSection>
        ) : (
          <Box variant="p" color="text-status-inactive">
            <FormattedMessage {...messages.noOutput} />
          </Box>
        )}
      </SpaceBetween>
    </div>
  );
};

export default StepDetails;
