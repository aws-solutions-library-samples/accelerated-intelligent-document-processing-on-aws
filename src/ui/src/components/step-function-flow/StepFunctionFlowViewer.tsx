/*
 * Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 * SPDX-License-Identifier: MIT-0
 */
import React, { useState, useEffect, useRef } from 'react';
import { defineMessages, FormattedMessage, useIntl, type NoMessageValues } from 'react-intl';
import { Container, Header, SpaceBetween, Box, Alert, Spinner, Button, Modal, Badge } from '@cloudscape-design/components';
import {
  FaPlay,
  FaCheck,
  FaTimes,
  FaClock,
  FaRobot,
  FaFileAlt,
  FaEye,
  FaChartBar,
  FaMap,
  FaList,
  FaExclamationTriangle,
} from 'react-icons/fa';
import { generateClient } from '../../api/client-shim';
import { ConsoleLogger } from 'aws-amplify/utils';

import { getStepFunctionExecution } from '../../graphql/generated';
import { getMalwareScanView } from '../common/malware-scan';
import type { MalwareScanFields } from '../common/malware-scan';
import FlowDiagram from './FlowDiagram';
import StepDetails from './StepDetails';
import { stepLabelMessages, stepName, stepStatusLabel } from './step-labels';
import { useFormatters } from '../../i18n/format';

import './StepFunctionFlowViewer.css';

const client = generateClient();
const logger = new ConsoleLogger('StepFunctionFlowViewer');

const messages = defineMessages<{
  title: NoMessageValues;
  autoRefreshOn: NoMessageValues;
  autoRefreshOff: NoMessageValues;
  loading: NoMessageValues;
  loadErrorHeader: NoMessageValues;
  loadErrorFallback: NoMessageValues;
  notFoundHeader: NoMessageValues;
  notFound: NoMessageValues;
  disableAutoRefresh: NoMessageValues;
  enableAutoRefresh: NoMessageValues;
  refreshNow: NoMessageValues;
  lastUpdated: { readonly time: string };
  overviewTitle: NoMessageValues;
  executionFailed: NoMessageValues;
  errorLabel: NoMessageValues;
  diagramTitle: NoMessageValues;
  stepDetailsTitle: NoMessageValues;
  timelineTitle: NoMessageValues;
  mapIteration: NoMessageValues;
}>({
  title: {
    id: 'documents.processingFlow.title',
    defaultMessage: 'Document Processing Flow',
    description:
      "Title of the dialog that Reports › Processing Flow opens on the document page, which shows the document's Step Functions execution.",
  },
  autoRefreshOn: {
    id: 'documents.processingFlow.autoRefreshOn',
    defaultMessage: '🔄 Auto-Refresh On',
    description:
      'Beside the refresh buttons in Reports › Processing Flow on the document page: the dialog reloads the execution every 10 seconds. Keep the emoji.',
  },
  autoRefreshOff: {
    id: 'documents.processingFlow.autoRefreshOff',
    defaultMessage: '⏸️ Auto-Refresh Off',
    description:
      'Beside the refresh buttons in Reports › Processing Flow on the document page: the dialog does not reload the execution by itself. Keep the emoji.',
  },
  loading: {
    id: 'documents.processingFlow.loading',
    defaultMessage: 'Loading processing flow...',
    description: "Shown in Reports › Processing Flow on the document page while the document's Step Functions execution loads.",
  },
  loadErrorHeader: {
    id: 'documents.processingFlow.loadErrorHeader',
    defaultMessage: 'Error loading processing flow',
    description: "Header of the alert in Reports › Processing Flow on the document page when the document's execution could not be loaded.",
  },
  loadErrorFallback: {
    id: 'documents.processingFlow.loadErrorFallback',
    defaultMessage: 'An error occurred while loading the processing flow.',
    description:
      "Text of the alert in Reports › Processing Flow on the document page when the document's execution could not be loaded and the error has no message of its own.",
  },
  notFoundHeader: {
    id: 'documents.processingFlow.notFoundHeader',
    defaultMessage: 'No processing flow found',
    description:
      'Header of the notice in Reports › Processing Flow on the document page when the document has no Step Functions execution.',
  },
  notFound: {
    id: 'documents.processingFlow.notFound',
    defaultMessage: 'No Step Functions execution found for this document.',
    description:
      'Text of the notice in Reports › Processing Flow on the document page when the document has no Step Functions execution. Step Functions is the name of an AWS service.',
  },
  disableAutoRefresh: {
    id: 'documents.processingFlow.disableAutoRefresh',
    defaultMessage: 'Disable Auto-Refresh',
    description: 'Button in Reports › Processing Flow on the document page that stops reloading the execution every 10 seconds.',
  },
  enableAutoRefresh: {
    id: 'documents.processingFlow.enableAutoRefresh',
    defaultMessage: 'Enable Auto-Refresh',
    description: 'Button in Reports › Processing Flow on the document page that starts reloading the execution every 10 seconds.',
  },
  refreshNow: {
    id: 'documents.processingFlow.refreshNow',
    defaultMessage: 'Refresh Now',
    description: 'Button in Reports › Processing Flow on the document page that reloads the execution once.',
  },
  lastUpdated: {
    id: 'documents.processingFlow.lastUpdated',
    defaultMessage: 'Last updated: {time}',
    description:
      'When Reports › Processing Flow on the document page last loaded the execution. {time} is the time of day, formatted for the UI language.',
  },
  overviewTitle: {
    id: 'documents.processingFlow.overviewTitle',
    defaultMessage: 'Execution Overview',
    description:
      "Header of the section of Reports › Processing Flow on the document page that shows the execution's status, duration, start time and end time.",
  },
  executionFailed: {
    id: 'documents.processingFlow.executionFailed',
    defaultMessage: 'Execution Failed',
    description:
      'Header of the alert in the Execution Overview of Reports › Processing Flow on the document page when the execution failed. The alert shows the error the execution reported, in English.',
  },
  errorLabel: {
    id: 'documents.processingFlow.errorLabel',
    defaultMessage: 'Error:',
    description:
      'Bold label before the error an execution or a step reported, in the Execution Overview and the Steps Timeline of Reports › Processing Flow on the document page. The error stays in English.',
  },
  diagramTitle: {
    id: 'documents.processingFlow.diagramTitle',
    defaultMessage: 'Processing Flow',
    description: "Header of the section of Reports › Processing Flow on the document page that draws the execution's steps as a diagram.",
  },
  stepDetailsTitle: {
    id: 'documents.processingFlow.stepDetailsTitle',
    defaultMessage: 'Step Details',
    description:
      'Header of the section of Reports › Processing Flow on the document page that shows the step chosen in the diagram or the Steps Timeline.',
  },
  timelineTitle: {
    id: 'documents.processingFlow.timelineTitle',
    defaultMessage: 'Steps Timeline',
    description:
      "Header of the section of Reports › Processing Flow on the document page that lists the execution's steps with their status and duration.",
  },
  mapIteration: {
    id: 'documents.processingFlow.mapIteration',
    defaultMessage: 'Map Iteration',
    description:
      'Badge on an entry of the Steps Timeline in Reports › Processing Flow on the document page that is one iteration of a Map step. Map is a Step Functions state type.',
  },
});

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
  input?: string;
  output?: string;
  error?: string;
  isMainStep?: boolean;
  isMapIteration?: boolean;
  parentMapName?: string;
  iterationIndex?: number;
  mapIterations?: number;
  mapIterationDetails?: Step[];
  /** Set on the Malware Scan step only: the document's verdict, which the step is recognised and worded by. */
  malwareScan?: MalwareScanFields;
}

interface ExecutionData {
  getStepFunctionExecution: {
    status: string;
    startDate?: string;
    stopDate?: string;
    error?: string;
    steps?: Step[];
  };
}

interface StepFunctionFlowViewerProps {
  executionArn: string;
  visible: boolean;
  onDismiss: () => void;
  mergedConfig?: StepConfig | null;
  /**
   * The document's malware-scan fields, when this deployment scans uploads.
   *
   * The gate is NOT a Step Functions state — it runs before the execution
   * starts, so an infected file never occupies a concurrency slot and its bytes
   * never reach OCR or Bedrock — which means it appears in no execution history
   * and the diagram would silently omit the one step that decided whether this
   * document ran at all. Passing the verdict in lets it be drawn as the first
   * step, where a reader expects to find it.
   */
  malwareScan?: MalwareScanFields | null;
}

/**
 * The `name` of the leading synthetic step, built from the document's own verdict. Like every
 * step's name, it keys the step's elements and matches the selected step. It is not shown:
 * `stepName` names the step in the UI's language.
 */
const MALWARE_SCAN_STEP_NAME = 'Malware Scan';

export const buildMalwareScanStep = (scan?: MalwareScanFields | null): Step | null => {
  if (!scan) return null;
  const view = getMalwareScanView(scan);
  if (view.outcome === 'none') return null;

  // Mapped to the vocabulary the real steps use, so every status-driven branch
  // in this component and in FlowDiagram treats it like any other step.
  //
  // ⚠️ **Only two of the four outcomes are reachable here**, and that is on
  // purpose. The viewer renders only when `executionArn` is set (see
  // `DocumentPanel.tsx`), and a document reaches this view only once a workflow
  // has run — so a `QUARANTINED` or `MALWARE_SCANNING` document never opens the
  // viewer at all. The two reachable cases are `clean` and the admitted
  // unscannable path (the config allows it under policy), and both are
  // successful first steps: the document went through. The `label` already
  // conveys the "admitted unscanned" caveat, so a red step would read as a
  // failure the document plainly did not have.
  const status = {
    clean: 'SUCCEEDED',
    unscannable: 'SUCCEEDED',
    // Not reachable, but mapped for completeness. The gate's refusal path is
    // surfaced by the detail-panel banner and the status badge, not here.
    threat: 'FAILED',
    pending: 'RUNNING',
  }[view.outcome];

  return {
    name: MALWARE_SCAN_STEP_NAME,
    type: 'Task',
    status,
    startDate: scan.malwareScanTime,
    stopDate: scan.malwareScanTime,
    isMainStep: true,
    // `output` rather than `error` even for a refusal: `error` is rendered as an
    // execution failure, and this execution did not fail — for a refused
    // document it never started. The detail panel's alert carries the remedy.
    output: view.detail,
    // The verdict marks the step: where it shows, `stepName` and `stepOutput` word it in the UI's
    // language, so this build, which runs inside the fetch, needs no `intl`.
    malwareScan: scan,
  };
};

// Helper function to check if a step is disabled based on configuration
const isStepDisabled = (name: string, config: StepConfig | null | undefined): boolean => {
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

const StepFunctionFlowViewer = ({
  executionArn,
  visible,
  onDismiss,
  mergedConfig = null,
  malwareScan = null,
}: StepFunctionFlowViewerProps): React.JSX.Element => {
  const intl = useIntl();
  const { formatDateTime, formatTime } = useFormatters();
  const [selectedStep, setSelectedStep] = useState<Step | null>(null);
  const [data, setData] = useState<ExecutionData | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<Record<string, unknown> | null>(null);
  const [autoRefreshEnabled, setAutoRefreshEnabled] = useState(false);
  const [lastRefreshTime, setLastRefreshTime] = useState<Date | null>(null);
  const autoRefreshIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const AUTO_REFRESH_INTERVAL = 10000; // 10 seconds
  const [processedSteps, setProcessedSteps] = useState<Step[]>([]);

  // Function to process Step Function steps for better visualization
  const processStepFunctionStepsData = (steps: Step[]): Step[] => {
    if (!steps || !Array.isArray(steps)) return [];

    // Create a flattened list of all steps including Map iterations
    const allSteps: Step[] = [];

    steps.forEach((step) => {
      // Add the main step
      allSteps.push({
        ...step,
        isMainStep: true,
      });

      // If this is a Map state with iterations, add them as separate steps
      if (step.type === 'Map' && step.mapIterationDetails && step.mapIterationDetails.length > 0) {
        step.mapIterationDetails.forEach((iteration, index) => {
          allSteps.push({
            ...iteration,
            isMapIteration: true,
            parentMapName: step.name,
            iterationIndex: index,
          });
        });
      }
    });

    // Sort by start date to maintain chronological order
    return allSteps.sort((a, b) => {
      if (!a.startDate) return 1;
      if (!b.startDate) return -1;
      return new Date(a.startDate).getTime() - new Date(b.startDate).getTime();
    });
  };

  const fetchStepFunctionExecution = async () => {
    if (!executionArn || !visible) return;

    setLoading(true);
    setError(null);

    try {
      // Add detailed logging for debugging
      logger.info('Fetching Step Function execution with ARN:', executionArn);
      console.log('Fetching Step Function execution with ARN:', executionArn);

      const result = await client.graphql({ query: getStepFunctionExecution, variables: { executionArn } });
      const resultData = (result as { data: Record<string, unknown> }).data;
      logger.info('GraphQL response received:', result);
      console.log('GraphQL response received:', result);

      setData(resultData as unknown as ExecutionData);
      setLastRefreshTime(new Date());
      logger.debug('Step Functions execution data:', resultData);

      // Process the steps to handle Map state visualization
      if (
        (resultData as Record<string, unknown>)?.getStepFunctionExecution &&
        ((resultData as Record<string, unknown>).getStepFunctionExecution as Record<string, unknown>)?.steps
      ) {
        const enhancedSteps = processStepFunctionStepsData(
          ((resultData as Record<string, unknown>).getStepFunctionExecution as Record<string, unknown>).steps as Step[],
        );
        // Prepended, not appended: the gate runs before the execution exists, so
        // first is where it belongs chronologically and where a reader looks for
        // "was this document allowed in".
        const malwareStep = buildMalwareScanStep(malwareScan);
        setProcessedSteps(malwareStep ? [malwareStep, ...enhancedSteps] : enhancedSteps);

        // If there's a failed step and no step is currently selected, select the first failed step
        if (!selectedStep) {
          const failedStep = enhancedSteps.find((step) => step.status === 'FAILED');
          if (failedStep) {
            setSelectedStep(failedStep);
            logger.info('Auto-selected failed step:', failedStep.name);
          }
        }
      }
    } catch (err) {
      logger.error('Error fetching Step Functions execution:', err);
      console.error('Error fetching Step Functions execution:', err);

      // Enhanced error details for debugging
      const graphqlErr = err as Record<string, unknown>;
      if (Array.isArray(graphqlErr.errors)) {
        graphqlErr.errors.forEach((errorItem: unknown, index: number) => {
          logger.error(`GraphQL Error ${index + 1}:`, errorItem);
          console.error(`GraphQL Error ${index + 1}:`, errorItem);
        });
      }

      setError(graphqlErr);
    } finally {
      setLoading(false);
    }
  };

  // Set up auto-refresh functionality
  const setupAutoRefresh = () => {
    if (autoRefreshEnabled) {
      // Clear any existing interval
      if (autoRefreshIntervalRef.current) {
        clearInterval(autoRefreshIntervalRef.current);
      }

      // Set up new interval
      autoRefreshIntervalRef.current = setInterval(() => {
        logger.debug('Auto-refreshing Step Functions execution data');
        fetchStepFunctionExecution();
      }, AUTO_REFRESH_INTERVAL);

      logger.info('Auto-refresh enabled for execution:', executionArn);
    } else if (autoRefreshIntervalRef.current) {
      // Disable auto-refresh by clearing the interval
      clearInterval(autoRefreshIntervalRef.current);
      autoRefreshIntervalRef.current = null;
      logger.info('Auto-refresh disabled for execution:', executionArn);
    }
  };

  // Toggle auto-refresh
  const toggleAutoRefresh = () => {
    const newState = !autoRefreshEnabled;
    setAutoRefreshEnabled(newState);
  };

  // Helper function to get status indicator
  const getStatusIndicator = () => {
    if (autoRefreshEnabled) {
      return (
        <Box variant="small" color="text-status-success">
          <FormattedMessage {...messages.autoRefreshOn} />
        </Box>
      );
    }
    return (
      <Box variant="small" color="text-status-inactive">
        <FormattedMessage {...messages.autoRefreshOff} />
      </Box>
    );
  };

  // Initial fetch and auto-refresh setup
  useEffect(() => {
    if (visible && executionArn) {
      fetchStepFunctionExecution();
    }

    return () => {
      // Clean up interval on unmount
      if (autoRefreshIntervalRef.current) {
        clearInterval(autoRefreshIntervalRef.current);
        autoRefreshIntervalRef.current = null;
      }
    };
  }, [executionArn, visible]);

  // Update auto-refresh when the toggle changes
  useEffect(() => {
    setupAutoRefresh();
  }, [autoRefreshEnabled]);

  // Disable auto-refresh when execution completes
  useEffect(() => {
    const execution = data?.getStepFunctionExecution;
    if (execution && (execution.status === 'SUCCEEDED' || execution.status === 'FAILED' || execution.status === 'ABORTED')) {
      // If execution is complete, disable auto-refresh
      if (autoRefreshEnabled) {
        logger.info('Execution complete, disabling auto-refresh');
        setAutoRefreshEnabled(false);
      }
    }
  }, [data]);

  const getStepIcon = (name: string, stepType: string, status: string): React.JSX.Element => {
    const iconProps = { size: 24, className: `step-icon step-icon-${status.toLowerCase()}` };

    if (name.toLowerCase().includes('upload') || name.toLowerCase().includes('input')) {
      return <FaFileAlt size={iconProps.size} className={iconProps.className} />;
    }
    if (name.toLowerCase().includes('bda') || name.toLowerCase().includes('bedrock')) {
      return <FaRobot size={iconProps.size} className={iconProps.className} />;
    }
    if (name.toLowerCase().includes('review') || name.toLowerCase().includes('hitl')) {
      return <FaEye size={iconProps.size} className={iconProps.className} />;
    }
    if (name.toLowerCase().includes('result') || name.toLowerCase().includes('process')) {
      return <FaChartBar size={iconProps.size} className={iconProps.className} />;
    }
    if (name.toLowerCase().includes('map') || name.toLowerCase().includes('sections')) {
      return <FaMap size={iconProps.size} className={iconProps.className} />;
    }
    if (name.toLowerCase().includes('iterator')) {
      return <FaList size={iconProps.size} className={iconProps.className} />;
    }
    if (name.toLowerCase().includes('ocr')) {
      return <FaFileAlt size={iconProps.size} className={iconProps.className} />;
    }
    if (name.toLowerCase().includes('classification')) {
      return <FaRobot size={iconProps.size} className={iconProps.className} />;
    }
    if (name.toLowerCase().includes('extraction')) {
      return <FaRobot size={iconProps.size} className={iconProps.className} />;
    }
    if (name.toLowerCase().includes('assessment')) {
      return <FaEye size={iconProps.size} className={iconProps.className} />;
    }
    if (name.toLowerCase().includes('summarization')) {
      return <FaChartBar size={iconProps.size} className={iconProps.className} />;
    }
    if (name.toLowerCase().includes('evaluation')) {
      return <FaChartBar size={iconProps.size} className={iconProps.className} />;
    }
    if (name.toLowerCase().includes('workflow')) {
      return <FaCheck size={iconProps.size} className={iconProps.className} />;
    }

    // Default icons based on status
    switch (status) {
      case 'SUCCEEDED':
        return <FaCheck size={iconProps.size} className={iconProps.className} />;
      case 'FAILED':
        return <FaTimes size={iconProps.size} className={iconProps.className} />;
      case 'RUNNING':
        return <FaClock size={iconProps.size} className={iconProps.className} />;
      default:
        return <FaPlay size={iconProps.size} className={iconProps.className} />;
    }
  };

  const formatDuration = (startDate?: string, stopDate?: string): string => {
    if (!startDate) return intl.formatMessage(stepLabelMessages.notAvailable);
    const start = new Date(startDate);
    const end = stopDate ? new Date(stopDate) : new Date();
    const duration = Math.floor((end.getTime() - start.getTime()) / 1000);

    const minutes = Math.floor(duration / 60);
    const seconds = duration % 60;
    return `${minutes}:${seconds.toString().padStart(2, '0')}`;
  };

  if (loading && !data) {
    return (
      <Modal visible={visible} onDismiss={onDismiss} header={intl.formatMessage(messages.title)} size="max">
        <Box textAlign="center" padding="xxl">
          <Spinner size="large" />
          <Box variant="p" margin={{ top: 'm' }}>
            <FormattedMessage {...messages.loading} />
          </Box>
        </Box>
      </Modal>
    );
  }

  if (error) {
    return (
      <Modal visible={visible} onDismiss={onDismiss} header={intl.formatMessage(messages.title)} size="max">
        <Alert type="error" header={intl.formatMessage(messages.loadErrorHeader)}>
          {String(error.message || intl.formatMessage(messages.loadErrorFallback))}
        </Alert>
      </Modal>
    );
  }

  const execution = data?.getStepFunctionExecution;
  if (!execution) {
    return (
      <Modal visible={visible} onDismiss={onDismiss} header={intl.formatMessage(messages.title)} size="max">
        <Alert type="info" header={intl.formatMessage(messages.notFoundHeader)}>
          <FormattedMessage {...messages.notFound} />
        </Alert>
      </Modal>
    );
  }

  // Check if execution has a top-level error
  const hasExecutionError = execution.status === 'FAILED' && execution.error;

  return (
    <Modal
      visible={visible}
      onDismiss={onDismiss}
      header={
        <Header
          variant="h2"
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              {getStatusIndicator()}
              <Button
                variant={autoRefreshEnabled ? 'primary' : 'normal'}
                onClick={toggleAutoRefresh}
                iconName={autoRefreshEnabled ? 'refresh' : 'settings'}
              >
                <FormattedMessage {...(autoRefreshEnabled ? messages.disableAutoRefresh : messages.enableAutoRefresh)} />
              </Button>
              <Button onClick={() => fetchStepFunctionExecution()} iconName="refresh" loading={loading}>
                <FormattedMessage {...messages.refreshNow} />
              </Button>
              {lastRefreshTime && (
                <Box variant="small" color="text-body-secondary">
                  <FormattedMessage {...messages.lastUpdated} values={{ time: formatTime(lastRefreshTime) }} />
                </Box>
              )}
            </SpaceBetween>
          }
        >
          <FormattedMessage {...messages.title} />
        </Header>
      }
      size="max"
    >
      <SpaceBetween size="l">
        {/* Execution Overview */}
        <Container
          header={
            <Header variant="h3">
              <FormattedMessage {...messages.overviewTitle} />
            </Header>
          }
        >
          <SpaceBetween size="l">
            <SpaceBetween direction="horizontal" size="l">
              <Box>
                <Box variant="awsui-key-label">
                  <FormattedMessage {...stepLabelMessages.status} />
                </Box>
                <Box className={`execution-status execution-status-${execution.status.toLowerCase()}`}>
                  {stepStatusLabel(execution.status, intl)}
                </Box>
              </Box>
              <Box>
                <Box variant="awsui-key-label">
                  <FormattedMessage {...stepLabelMessages.duration} />
                </Box>
                <Box>{formatDuration(execution.startDate, execution.stopDate)}</Box>
              </Box>
              <Box>
                <Box variant="awsui-key-label">
                  <FormattedMessage {...stepLabelMessages.started} />
                </Box>
                <Box>
                  {execution.startDate ? (
                    formatDateTime(new Date(execution.startDate))
                  ) : (
                    <FormattedMessage {...stepLabelMessages.notAvailable} />
                  )}
                </Box>
              </Box>
              {execution.stopDate && (
                <Box>
                  <Box variant="awsui-key-label">
                    <FormattedMessage {...stepLabelMessages.completed} />
                  </Box>
                  <Box>{formatDateTime(new Date(execution.stopDate))}</Box>
                </Box>
              )}
            </SpaceBetween>

            {/* Display execution-level error if present */}
            {hasExecutionError && (
              <Alert type="error" header={intl.formatMessage(messages.executionFailed)}>
                <SpaceBetween size="s">
                  <Box>
                    <FaExclamationTriangle size={20} style={{ marginRight: '8px', color: '#d13212' }} />
                    <strong>
                      <FormattedMessage {...messages.errorLabel} />
                    </strong>{' '}
                    {execution.error}
                  </Box>
                </SpaceBetween>
              </Alert>
            )}
          </SpaceBetween>
        </Container>

        {/* Flow Diagram */}
        <Container
          header={
            <Header variant="h3">
              <FormattedMessage {...messages.diagramTitle} />
            </Header>
          }
        >
          <FlowDiagram
            steps={processedSteps.length > 0 ? processedSteps : execution.steps || []}
            onStepClick={setSelectedStep}
            selectedStep={selectedStep}
            getStepIcon={getStepIcon}
            mergedConfig={mergedConfig}
          />
        </Container>

        {/* Step Details */}
        {selectedStep && (
          <Container
            header={
              <Header variant="h3">
                <FormattedMessage {...messages.stepDetailsTitle} />
              </Header>
            }
          >
            <StepDetails step={selectedStep} formatDuration={formatDuration} getStepIcon={getStepIcon} mergedConfig={mergedConfig} />
          </Container>
        )}

        {/* Steps Timeline */}
        <Container
          header={
            <Header variant="h3">
              <FormattedMessage {...messages.timelineTitle} />
            </Header>
          }
        >
          <div className="steps-timeline">
            {(processedSteps.length > 0 ? processedSteps : execution.steps)?.map((step, index) => {
              const stepDisabled = isStepDisabled(step.name, mergedConfig);
              return (
                <div
                  key={`timeline-${step.name}-${step.type}-${step.status}-${step.startDate || index}`}
                  className={`timeline-step ${step.status.toLowerCase()} ${selectedStep?.name === step.name ? 'selected' : ''} ${
                    step.isMapIteration ? 'map-iteration-step' : ''
                  } ${stepDisabled ? 'step-disabled' : ''}`}
                  onClick={() => setSelectedStep(step)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' || e.key === ' ') {
                      setSelectedStep(step);
                    }
                  }}
                  role="button"
                  tabIndex={0}
                  title={stepDisabled ? intl.formatMessage(stepLabelMessages.disabledTitle) : ''}
                >
                  <div className="timeline-step-icon">{getStepIcon(step.name, step.type, step.status)}</div>
                  <div className="timeline-step-content">
                    <div className="timeline-step-name">
                      {stepName(step, intl)}
                      {stepDisabled && (
                        <Badge color="grey">
                          <FormattedMessage {...stepLabelMessages.notEnabled} />
                        </Badge>
                      )}
                      {step.isMapIteration && (
                        <Badge color="green">
                          <FormattedMessage {...messages.mapIteration} />
                        </Badge>
                      )}
                      {step.type === 'Map' && step.mapIterations && (
                        <Badge color="blue">
                          <FormattedMessage {...stepLabelMessages.iterations} values={{ count: step.mapIterations }} />
                        </Badge>
                      )}
                    </div>
                    <div className="timeline-step-meta">
                      <span className={`timeline-step-status status-${step.status.toLowerCase()}`}>
                        {stepStatusLabel(step.status, intl)}
                      </span>
                      <span className="timeline-step-duration">{formatDuration(step.startDate, step.stopDate)}</span>
                    </div>
                    {step.error && (
                      <div className="timeline-step-error">
                        <FaExclamationTriangle size={14} style={{ marginRight: '4px', color: '#d13212' }} />
                        <strong>
                          <FormattedMessage {...messages.errorLabel} />
                        </strong>{' '}
                        {step.error.length > 100 ? `${step.error.substring(0, 100)}...` : step.error}
                      </div>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </Container>
      </SpaceBetween>
    </Modal>
  );
};

export default StepFunctionFlowViewer;
