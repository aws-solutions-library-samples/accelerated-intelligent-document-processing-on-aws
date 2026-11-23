// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React, { useState } from 'react';
import { Box, SpaceBetween, ExpandableSection, Button, Alert, Container } from '@cloudscape-design/components';
import './StepDetails.css';
import { parseStepFunctionPayload } from '../../graphql/awsjson-parsers';
import { useFormatters } from '../../i18n/format';
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

const JsonDisplay = ({ data = null }: JsonDisplayProps): React.JSX.Element | null => {
  if (!data) return null;

  const formatJson = (jsonString: string | Record<string, unknown> | null | undefined): string => {
    if (!jsonString) return 'No data available';

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
  const { formatDateTime } = useFormatters();
  const [inputExpanded, setInputExpanded] = useState(false);
  const [outputExpanded, setOutputExpanded] = useState(false);
  const [errorExpanded, setErrorExpanded] = useState(true); // Default to expanded for errors

  const stepSkipped = step.status === SKIPPED_STATUS;
  const stepDisabled = !stepSkipped && isStepDisabled(step.name, mergedConfig);

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
                <Box variant="h3">{step.name}</Box>
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
              <Box variant="h3">{step.name}</Box>
              <Box variant="small" color="text-status-inactive">
                Type: {step.type}
              </Box>
            </div>
          </SpaceBetween>
        </Box>

        {/* Configuration Disabled Notice */}
        {stepDisabled && (
          <Alert type="info" header="Step Disabled in Configuration">
            This step was disabled in the configuration (<strong>enabled: false</strong>) and performed no processing. While the step
            function executed this step, the Lambda function detected the disabled state and skipped all processing logic.
          </Alert>
        )}

        {/* Step Metadata */}
        <div className="step-metadata">
          <SpaceBetween direction="horizontal" size="l">
            <Box>
              <Box variant="awsui-key-label">Status</Box>
              <Box className={`step-status step-status-${step.status.toLowerCase()}`}>{step.status}</Box>
            </Box>
            <Box>
              <Box variant="awsui-key-label">Duration</Box>
              <Box>{formatDuration(step.startDate, step.stopDate)}</Box>
            </Box>
            <Box>
              <Box variant="awsui-key-label">Started</Box>
              <Box>{step.startDate ? formatDateTime(new Date(step.startDate)) : 'N/A'}</Box>
            </Box>
            {step.stopDate && (
              <Box>
                <Box variant="awsui-key-label">Completed</Box>
                <Box>{formatDateTime(new Date(step.stopDate))}</Box>
              </Box>
            )}
          </SpaceBetween>
        </div>

        {/* Error Information */}
        {step.error && (
          <ExpandableSection
            headerText="Step Error"
            {...({ variant: 'error' } as Record<string, unknown>)}
            expanded={errorExpanded}
            onChange={({ detail }) => setErrorExpanded(detail.expanded)}
            headerActions={
              <Button
                variant="inline-icon"
                iconName="copy"
                onClick={() => copyToClipboard(step.error ?? '')}
                ariaLabel="Copy error message"
              />
            }
          >
            <Alert type="error" header="Error Details">
              <Box>
                <pre className="error-message">{step.error}</pre>
              </Box>
            </Alert>
          </ExpandableSection>
        )}

        {/* Input Data */}
        {step.input ? (
          <ExpandableSection
            headerText="Input Data"
            expanded={inputExpanded}
            onChange={({ detail }) => setInputExpanded(detail.expanded)}
            headerActions={
              <Button
                variant="inline-icon"
                iconName="copy"
                onClick={() => copyToClipboard(formatJson(step.input))}
                ariaLabel="Copy input data"
              />
            }
          >
            <JsonDisplay data={step.input} />
          </ExpandableSection>
        ) : (
          <Box variant="p" color="text-status-inactive">
            No input data available for this step.
          </Box>
        )}

        {/* Map Iterations */}
        {step.type === 'Map' && step.mapIterationDetails && step.mapIterationDetails.length > 0 && (
          <ExpandableSection headerText={`Map Iterations (${step.mapIterationDetails.length})`} expanded={false}>
            <SpaceBetween size="m">
              {step.mapIterationDetails.map((iteration, index) => (
                <Container key={`iteration-${iteration.name}-${iteration.startDate || index}`} header={iteration.name}>
                  <SpaceBetween size="s">
                    <div className="iteration-metadata">
                      <SpaceBetween direction="horizontal" size="m">
                        <Box>
                          <Box variant="awsui-key-label">Status</Box>
                          <Box className={`step-status step-status-${iteration.status.toLowerCase()}`}>{iteration.status}</Box>
                        </Box>
                        <Box>
                          <Box variant="awsui-key-label">Duration</Box>
                          <Box>{formatDuration(iteration.startDate, iteration.stopDate)}</Box>
                        </Box>
                        <Box>
                          <Box variant="awsui-key-label">Started</Box>
                          <Box>{iteration.startDate ? formatDateTime(new Date(iteration.startDate)) : 'N/A'}</Box>
                        </Box>
                        {iteration.stopDate && (
                          <Box>
                            <Box variant="awsui-key-label">Completed</Box>
                            <Box>{formatDateTime(new Date(iteration.stopDate))}</Box>
                          </Box>
                        )}
                      </SpaceBetween>
                    </div>
                    {iteration.error && (
                      <Alert type="error" header="Iteration Error">
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
        {step.output ? (
          <ExpandableSection
            headerText="Output Data"
            expanded={outputExpanded}
            onChange={({ detail }) => setOutputExpanded(detail.expanded)}
            headerActions={
              <Button
                variant="inline-icon"
                iconName="copy"
                onClick={() => copyToClipboard(formatJson(step.output))}
                ariaLabel="Copy output data"
              />
            }
          >
            <JsonDisplay data={step.output} />
          </ExpandableSection>
        ) : (
          <Box variant="p" color="text-status-inactive">
            No output data available for this step.
          </Box>
        )}
      </SpaceBetween>
    </div>
  );
};

export default StepDetails;
