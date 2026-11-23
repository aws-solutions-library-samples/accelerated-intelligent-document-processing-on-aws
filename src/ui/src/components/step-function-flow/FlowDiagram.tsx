// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React from 'react';
import { Box, Badge } from '@cloudscape-design/components';
import './FlowDiagram.css';
import { isStepDisabled, type StepConfig } from './is-step-disabled';
import SkippedStatus from './SkippedStatus';
import { SKIPPED_STATUS } from './skipped-steps';
import { withWordBreaks } from './word-breaks';

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
  /** Set on the entry of a step the run plan skipped: why it was not done. */
  skipNote?: string;
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

const FlowDiagram = ({
  steps = [],
  onStepClick,
  selectedStep = null,
  getStepIcon,
  mergedConfig = null,
}: FlowDiagramProps): React.JSX.Element => {
  if (!steps || steps.length === 0) {
    return (
      <Box textAlign="center" padding="xl">
        <Box variant="p" color="text-status-inactive">
          No steps available
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
          // A step the run plan skipped is greyed out like a disabled one, and says Skipped.
          const stepSkipped = step.status === SKIPPED_STATUS;
          const stepDisabled = !stepSkipped && isStepDisabled(step.name, mergedConfig);

          return (
            <React.Fragment key={`main-step-${step.name}-${step.type}-${step.startDate || index}`}>
              {/* Step Node */}
              <div
                className={`flow-step ${getStepStatus(step)} ${selectedStep?.name === step.name ? 'selected' : ''} ${
                  isMapState ? 'map-step' : ''
                } ${stepDisabled || stepSkipped ? 'step-disabled' : ''}`}
                onClick={() => onStepClick(step)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    onStepClick(step);
                  }
                }}
                role="button"
                tabIndex={0}
                title={stepDisabled ? 'This step was disabled in configuration and performed no processing' : (step.skipNote ?? '')}
              >
                <div className="step-icon-container">
                  {getStepIcon(step.name, step.type, step.status)}
                  {step.status === 'RUNNING' && <div className="step-pulse-ring" />}
                </div>
                <div className="step-label">
                  <div className="step-name">
                    <span>{withWordBreaks(step.name)}</span>
                    {stepDisabled && <Badge color="grey">NOT ENABLED</Badge>}
                    {isMapState && step.mapIterations && <Badge color="blue">{step.mapIterations} iterations</Badge>}
                  </div>
                  <div className={`step-status-text status-text-${step.status.toLowerCase()}`}>
                    {stepSkipped ? <SkippedStatus /> : step.status}
                  </div>
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
                    <h5>Map Iterations ({mapIterationsForThisStep.length})</h5>
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
                          <div className="iteration-name">{iteration.name}</div>
                          <div className={`iteration-status status-text-${iteration.status.toLowerCase()}`}>{iteration.status}</div>
                        </div>
                      </div>
                    ))}
                    {mapIterationsForThisStep.length > 6 && (
                      <div className="more-iterations">
                        <span>+{mapIterationsForThisStep.length - 6} more</span>
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
          <span>Completed</span>
        </div>
        <div className="legend-item">
          <div className="legend-icon running" />
          <span>Running</span>
        </div>
        <div className="legend-item">
          <div className="legend-icon failed" />
          <span>Failed</span>
        </div>
        <div className="legend-item">
          <div className="legend-icon pending" />
          <span>Pending</span>
        </div>
      </div>
    </div>
  );
};

export default FlowDiagram;
