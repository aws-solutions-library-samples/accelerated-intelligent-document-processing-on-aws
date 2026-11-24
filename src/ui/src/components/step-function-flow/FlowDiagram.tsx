// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React from 'react';
import { defineMessages, FormattedMessage, useIntl, type NoMessageValues } from 'react-intl';
import { Box, Badge } from '@cloudscape-design/components';
import './FlowDiagram.css';
import type { MalwareScanFields } from '../common/malware-scan';
import { stepLabelMessages, stepName, stepStatusLabel } from './step-labels';
import { withWordBreaks } from './word-breaks';

interface StepConfig {
  summarization?: { enabled?: boolean };
  // v0.6: confidence enablement lives under extraction.confidence; the slim
  // assessment carrier still exposes `enabled` for the step itself.
  extraction?: { confidence?: { enabled?: boolean } };
  assessment?: { enabled?: boolean };
  evaluation?: { enabled?: boolean };
}

interface Step {
  name: string;
  type: string;
  status: string;
  startDate?: string;
  stopDate?: string;
  error?: string;
  isMapIteration?: boolean;
  parentMapName?: string;
  mapIterations?: number;
  mapIterationDetails?: Step[];
  /** Set on the Malware Scan step only: the document's verdict, which the step is recognised and worded by. */
  malwareScan?: MalwareScanFields;
}

interface FlowDiagramProps {
  steps?: Step[];
  onStepClick: (step: Step) => void;
  selectedStep?: Step | null;
  getStepIcon: (name: string, type: string, status: string) => React.ReactNode;
  // Merged config for the version this document was processed with (passed
  // down from DocumentPanel via StepFunctionFlowViewer). Drives step-disabled
  // rendering so a historical run's greyed-out steps reflect the toggles
  // active AT PROCESSING TIME, not the stack's current live config.
  mergedConfig?: StepConfig | null;
}

const messages = defineMessages<{
  noSteps: NoMessageValues;
  mapIterations: { readonly count: number };
  moreIterations: { readonly count: number };
  legendCompleted: NoMessageValues;
  legendRunning: NoMessageValues;
  legendFailed: NoMessageValues;
  legendPending: NoMessageValues;
}>({
  noSteps: {
    id: 'documents.processingFlow.diagram.noSteps',
    defaultMessage: 'No steps available',
    description: 'Shown in the flow diagram of Reports › Processing Flow on the document page when the execution has no steps.',
  },
  mapIterations: {
    id: 'documents.processingFlow.diagram.mapIterations',
    defaultMessage: 'Map Iterations ({count})',
    description:
      'Heading over the iterations of a Map step in the flow diagram of Reports › Processing Flow on the document page. {count} is the number of iterations. Map is a Step Functions state type.',
  },
  moreIterations: {
    id: 'documents.processingFlow.diagram.moreIterations',
    defaultMessage: '+{count} more',
    description:
      'After the first six iterations of a Map step in the flow diagram of Reports › Processing Flow on the document page. {count} is the number of further iterations, which the diagram does not draw.',
  },
  legendCompleted: {
    id: 'documents.processingFlow.diagram.legendCompleted',
    defaultMessage: 'Completed',
    description: 'Legend of the flow diagram in Reports › Processing Flow on the document page: the colour of a step that succeeded.',
  },
  legendRunning: {
    id: 'documents.processingFlow.diagram.legendRunning',
    defaultMessage: 'Running',
    description: 'Legend of the flow diagram in Reports › Processing Flow on the document page: the colour of a step that is running.',
  },
  legendFailed: {
    id: 'documents.processingFlow.diagram.legendFailed',
    defaultMessage: 'Failed',
    description: 'Legend of the flow diagram in Reports › Processing Flow on the document page: the colour of a step that failed.',
  },
  legendPending: {
    id: 'documents.processingFlow.diagram.legendPending',
    defaultMessage: 'Pending',
    description: 'Legend of the flow diagram in Reports › Processing Flow on the document page: the colour of a pending step.',
  },
});

// Helper function to check if a step is disabled based on configuration
const isStepDisabled = (name: string, config: StepConfig | null): boolean => {
  if (!config) return false;

  const stepNameLower = name.toLowerCase();

  // Check if this is a summarization step
  if (stepNameLower.includes('summarization') || stepNameLower.includes('summary')) {
    return config.summarization?.enabled === false;
  }

  // Check if this is an assessment step
  if (stepNameLower.includes('assessment') || stepNameLower.includes('assess')) {
    return (config.extraction?.confidence?.enabled ?? config.assessment?.enabled) === false;
  }

  // Check if this is an evaluation step
  if (stepNameLower.includes('evaluation') || stepNameLower.includes('evaluate')) {
    return config.evaluation?.enabled === false;
  }

  return false;
};

const FlowDiagram = ({
  steps = [],
  onStepClick,
  selectedStep = null,
  getStepIcon,
  mergedConfig = null,
}: FlowDiagramProps): React.JSX.Element => {
  const intl = useIntl();
  if (!steps || steps.length === 0) {
    return (
      <Box textAlign="center" padding="xl">
        <Box variant="p" color="text-status-inactive">
          <FormattedMessage {...messages.noSteps} />
        </Box>
      </Box>
    );
  }

  // Separate main steps from Map iterations
  const mainSteps = steps.filter((step) => !step.isMapIteration);
  const mapIterations = steps.filter((step) => step.isMapIteration);

  const getStepStatus = (step: Step): string => {
    return step.status.toLowerCase();
  };

  const getProgressPercentage = (step: Step): number => {
    if (step.status === 'SUCCEEDED') return 100;
    if (step.status === 'FAILED') return 100;
    if (step.status === 'RUNNING') return 75; // Assume 75% for running steps
    return 0;
  };

  const getProgressBarStyle = (step: Step): React.CSSProperties => {
    const width = `${getProgressPercentage(step)}%`;

    // Add specific styling for failed steps
    if (step.status === 'FAILED') {
      return {
        width,
        backgroundColor: '#dc3545', // Red color for failed steps
      };
    }

    return { width };
  };

  // Group Map iterations by their parent Map state
  const mapIterationsByParent = mapIterations.reduce<Record<string, Step[]>>((acc, iteration) => {
    const parentName = iteration.parentMapName || 'Unknown';
    if (!acc[parentName]) {
      acc[parentName] = [];
    }
    acc[parentName].push(iteration);
    return acc;
  }, {});

  return (
    <div className="flow-diagram">
      <div className="flow-container">
        {mainSteps.map((step, index) => {
          // Check if this is a Map state
          const isMapState = step.type === 'Map';
          const mapIterationsForThisStep = mapIterationsByParent[step.name] || [];
          const stepDisabled = isStepDisabled(step.name, mergedConfig);

          return (
            <React.Fragment key={`main-step-${step.name}-${step.type}-${step.startDate || index}`}>
              {/* Step Node */}
              <div
                className={`flow-step ${getStepStatus(step)} ${selectedStep?.name === step.name ? 'selected' : ''} ${
                  isMapState ? 'map-step' : ''
                } ${stepDisabled ? 'step-disabled' : ''}`}
                onClick={() => onStepClick(step)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    onStepClick(step);
                  }
                }}
                role="button"
                tabIndex={0}
                title={stepDisabled ? intl.formatMessage(stepLabelMessages.disabledTitle) : ''}
              >
                <div className="step-icon-container">
                  {getStepIcon(step.name, step.type, step.status)}
                  {step.status === 'RUNNING' && <div className="step-pulse-ring" />}
                </div>
                <div className="step-label">
                  <div className="step-name">
                    <span>{withWordBreaks(stepName(step, intl))}</span>
                    {stepDisabled && (
                      <Badge color="grey">
                        <FormattedMessage {...stepLabelMessages.notEnabled} />
                      </Badge>
                    )}
                    {isMapState && step.mapIterations && (
                      <Badge color="blue">
                        <FormattedMessage {...stepLabelMessages.iterations} values={{ count: step.mapIterations }} />
                      </Badge>
                    )}
                  </div>
                  <div className={`step-status-text status-text-${step.status.toLowerCase()}`}>{stepStatusLabel(step.status, intl)}</div>
                  {step.error && (
                    <div className="step-error-indicator">
                      <span className="error-icon">⚠️</span>
                    </div>
                  )}
                </div>
                <div className="step-progress">
                  <div className={`step-progress-bar ${step.status.toLowerCase()}`} style={getProgressBarStyle(step)} />
                </div>
              </div>

              {/* Show Map iterations inline if this is a Map state */}
              {isMapState && mapIterationsForThisStep.length > 0 && (
                <div className="map-iterations-inline">
                  <div className="map-iterations-header">
                    <h5>
                      <FormattedMessage {...messages.mapIterations} values={{ count: mapIterationsForThisStep.length }} />
                    </h5>
                  </div>
                  <div className="map-iterations-grid">
                    {mapIterationsForThisStep.slice(0, 6).map((iteration, iterIndex) => (
                      <div
                        key={`iteration-${iteration.name}-${iteration.startDate || iterIndex}`}
                        className={`iteration-step ${getStepStatus(iteration)} ${selectedStep?.name === iteration.name ? 'selected' : ''}`}
                        onClick={() => onStepClick(iteration)}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter' || e.key === ' ') {
                            onStepClick(iteration);
                          }
                        }}
                        role="button"
                        tabIndex={0}
                      >
                        <div className="iteration-icon">{getStepIcon(iteration.name, iteration.type, iteration.status)}</div>
                        <div className="iteration-label">
                          <div className="iteration-name">{stepName(iteration, intl)}</div>
                          <div className={`iteration-status status-text-${iteration.status.toLowerCase()}`}>
                            {stepStatusLabel(iteration.status, intl)}
                          </div>
                        </div>
                      </div>
                    ))}
                    {mapIterationsForThisStep.length > 6 && (
                      <div className="more-iterations">
                        <span>
                          <FormattedMessage {...messages.moreIterations} values={{ count: mapIterationsForThisStep.length - 6 }} />
                        </span>
                      </div>
                    )}
                  </div>
                </div>
              )}

              {/* Flow Arrow */}
              {index < mainSteps.length - 1 && (
                <div className="flow-arrow">
                  <div className="arrow-line">
                    <div className="arrow-animation" />
                  </div>
                  <div className="arrow-head" />
                </div>
              )}
            </React.Fragment>
          );
        })}
      </div>

      {/* Complete Flow Section */}
      <div className="flow-legend">
        <div className="legend-item">
          <div className="legend-icon succeeded" />
          <span>
            <FormattedMessage {...messages.legendCompleted} />
          </span>
        </div>
        <div className="legend-item">
          <div className="legend-icon running" />
          <span>
            <FormattedMessage {...messages.legendRunning} />
          </span>
        </div>
        <div className="legend-item">
          <div className="legend-icon failed" />
          <span>
            <FormattedMessage {...messages.legendFailed} />
          </span>
        </div>
        <div className="legend-item">
          <div className="legend-icon pending" />
          <span>
            <FormattedMessage {...messages.legendPending} />
          </span>
        </div>
      </div>
    </div>
  );
};

export default FlowDiagram;
