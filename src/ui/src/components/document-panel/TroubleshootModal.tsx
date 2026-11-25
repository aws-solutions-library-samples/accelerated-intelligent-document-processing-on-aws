// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React, { useState, useEffect } from 'react';
import { generateClient } from '../../api/client-shim';
import { ConsoleLogger } from 'aws-amplify/utils';
import { Modal, Box, SpaceBetween, Button, Spinner, Alert, Header } from '@cloudscape-design/components';
import {
  defineMessages,
  FormattedMessage,
  useIntl,
  type IntlShape,
  type MessageTag,
  type NoMessageValues,
  type TypedMessageDescriptor,
} from 'react-intl';

import { submitAgentQuery, getAgentJobStatus, listAvailableAgents } from '../../graphql/generated';
import AgentResultDisplay from '../document-agents-layout/AgentResultDisplay';
import AgentMessagesDisplay from '../document-agents-layout/AgentMessagesDisplay';
import './TroubleshootModal.css';
import useDeploymentContext from '../../hooks/use-deployment-context';
import { buildBugReportUrl, buildFullDetailsText, type DocumentContext } from '../../utils/github-feedback';
import { extractFindingsText } from './troubleshootFindings';

interface DocumentItem {
  objectKey: string;
  objectStatus?: string;
  [key: string]: unknown;
}

/**
 * The job's error: the server's or a thrown error's own text, kept as it arrives, or one of the
 * dialog's messages, worded at render so it follows a language switch. The minimized chip keeps
 * the job running while the language menu stays usable.
 */
export type TroubleshootError = string | { message: TypedMessageDescriptor<NoMessageValues> };

interface ExistingJob {
  jobId: string;
  status: string;
  result?: string | Record<string, unknown>;
  agentMessages?: unknown[] | Record<string, unknown>;
  error?: TroubleshootError;
  timestamp?: number;
  documentKey?: string;
}

interface AgentInfo {
  agent_id: string;
  [key: string]: unknown;
}

interface TroubleshootModalProps {
  visible: boolean;
  onDismiss: () => void;
  documentItem?: DocumentItem | null;
  existingJob?: ExistingJob | null;
  onJobUpdate?:
    | ((jobData: {
        jobId: string;
        status: string | null;
        result: string | Record<string, unknown> | null;
        agentMessages: unknown;
        error: TroubleshootError | null;
        timestamp: number;
        documentKey: string | undefined;
      }) => void)
    | null;
}

interface Subscription {
  unsubscribe: () => void;
}

const client = generateClient();
const logger = new ConsoleLogger('TroubleshootModal');

const messages = defineMessages<{
  submitFailed: NoMessageValues;
  jobFailed: NoMessageValues;
  restoreAriaLabel: NoMessageValues;
  running: NoMessageValues;
  ready: NoMessageValues;
  title: NoMessageValues;
  copied: NoMessageValues;
  copy: NoMessageValues;
  report: NoMessageValues;
  runAgain: NoMessageValues;
  minimize: NoMessageValues;
  close: NoMessageValues;
  analyzing: { readonly objectKey: string | undefined; readonly strong: MessageTag };
  analyzingFailure: NoMessageValues;
  status: { readonly status: string };
}>({
  submitFailed: {
    id: 'documents.troubleshoot.submitFailed',
    defaultMessage: 'Failed to submit troubleshoot query',
    description: 'Error in the Troubleshoot dialog when the troubleshooting request could not be sent and gave no reason.',
  },
  jobFailed: {
    id: 'documents.troubleshoot.jobFailed',
    defaultMessage: 'Job processing failed',
    description: 'Error in the Troubleshoot dialog when the troubleshooting job failed and gave no reason.',
  },
  restoreAriaLabel: {
    id: 'documents.troubleshoot.restoreAriaLabel',
    defaultMessage: 'Restore Troubleshoot window',
    description: 'Screen-reader label of the chip that reopens the minimized Troubleshoot dialog.',
  },
  running: {
    id: 'documents.troubleshoot.running',
    defaultMessage: 'Troubleshooting…',
    description: 'Text of the minimized Troubleshoot chip while the analysis runs.',
  },
  ready: {
    id: 'documents.troubleshoot.ready',
    defaultMessage: 'Troubleshoot results ready',
    description: 'Text of the minimized Troubleshoot chip once the analysis has finished.',
  },
  title: {
    id: 'documents.troubleshoot.title',
    defaultMessage: 'Troubleshoot Document',
    description: 'Title of the Troubleshoot dialog, which asks an agent why a document failed.',
  },
  copied: {
    id: 'documents.troubleshoot.copied',
    defaultMessage: 'Copied',
    description: 'Copy full details button of the Troubleshoot dialog, just after copying.',
  },
  copy: {
    id: 'documents.troubleshoot.copy',
    defaultMessage: 'Copy full details',
    description: 'Button of the Troubleshoot dialog that copies the findings and deployment details.',
  },
  report: {
    id: 'documents.troubleshoot.report',
    defaultMessage: 'Report this issue on GitHub',
    description: 'Button of the Troubleshoot dialog that opens a prefilled GitHub issue.',
  },
  runAgain: {
    id: 'documents.troubleshoot.runAgain',
    defaultMessage: 'Run again',
    description: 'Button of the Troubleshoot dialog that starts a new analysis.',
  },
  minimize: {
    id: 'documents.troubleshoot.minimize',
    defaultMessage: 'Minimize',
    description: 'Button of the Troubleshoot dialog that collapses it to a chip while the analysis runs.',
  },
  close: {
    id: 'documents.troubleshoot.close',
    defaultMessage: 'Close',
    description: 'Button that closes the Troubleshoot dialog.',
  },
  analyzing: {
    id: 'documents.troubleshoot.analyzing',
    defaultMessage: 'Analyzing document: <strong>{objectKey}</strong>',
    description: 'Notice at the top of the Troubleshoot dialog. {objectKey} is the document key, shown as is.',
  },
  analyzingFailure: {
    id: 'documents.troubleshoot.analyzingFailure',
    defaultMessage: 'Analyzing document failure...',
    description: 'Shown under the spinner while the troubleshooting request is being sent.',
  },
  status: {
    id: 'documents.troubleshoot.status',
    defaultMessage: 'Status: {status}',
    description: 'Status of the troubleshooting job. {status} is the job status code (PENDING, PROCESSING, COMPLETED), shown as is.',
  },
});

/** Renders a `<strong>...</strong>` element value: the document key. */
const strong = (chunks: React.ReactNode[]): React.JSX.Element => <strong>{chunks}</strong>;

/** The job's error in the UI language. */
const errorText = (error: TroubleshootError | null, intl: IntlShape): string | null =>
  error === null || typeof error === 'string' ? error : intl.formatMessage(error.message);

const TroubleshootModal = ({
  visible,
  onDismiss,
  documentItem = null,
  existingJob = null,
  onJobUpdate = null,
}: TroubleshootModalProps): React.JSX.Element => {
  const [jobId, setJobId] = useState<string | null>(null);
  const [jobStatus, setJobStatus] = useState<string | null>(null);
  const [jobResult, setJobResult] = useState<string | Record<string, unknown> | null>(null);
  const [agentMessages, setAgentMessages] = useState<unknown>(null);
  const [error, setError] = useState<TroubleshootError | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [subscription, setSubscription] = useState<Subscription | null>(null);
  const [_availableAgents, setAvailableAgents] = useState<AgentInfo[]>([]);
  const [copied, setCopied] = useState(false);
  // When minimized the modal collapses to a small restore chip so the
  // analysis keeps running (and polling continues) instead of being torn down.
  const [minimized, setMinimized] = useState(false);

  const intl = useIntl();
  const deploymentContext = useDeploymentContext();

  // English on purpose: the request the agent reads, which its results also print as "Results for: …".
  const query = `Troubleshoot ${documentItem?.objectKey} for failures or performance issues.`;

  // A terminal (COMPLETED/FAILED) job with either findings or an error is worth
  // reporting. Build the document context once so the "Report this issue"
  // button and "Copy full details" share the same data.
  const isTerminal = jobStatus === 'COMPLETED' || jobStatus === 'FAILED';
  const findings = extractFindingsText(jobResult);
  const shownError = errorText(error, intl);
  const canReport = isTerminal && (Boolean(findings) || Boolean(error));

  const docContext: DocumentContext = {
    objectKey: documentItem?.objectKey,
    objectStatus: documentItem?.objectStatus,
    configVersion: documentItem?.configVersion as string | undefined,
    executionArn: documentItem?.executionArn as string | undefined,
    jobError: shownError ?? undefined,
    findings: findings || undefined,
  };

  const reportIssueUrl = buildBugReportUrl(deploymentContext, docContext);

  const handleCopyFullDetails = async (): Promise<void> => {
    const text = buildFullDetailsText(deploymentContext, docContext);
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      // Fallback for older browsers / insecure contexts.
      const textarea = document.createElement('textarea');
      textarea.value = text;
      textarea.style.position = 'fixed';
      textarea.style.opacity = '0';
      document.body.appendChild(textarea);
      textarea.select();
      try {
        document.execCommand('copy');
      } catch (err) {
        logger.error('Failed to copy troubleshoot details:', err);
      }
      document.body.removeChild(textarea);
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  // AppSync subscriptions were removed; agent job completion is detected by the
  // interval polling effect below (polls getAgentJobStatus until terminal). This
  // is a no-op retained so existing call sites stay unchanged.
  const subscribeToJobCompletion = (id: string): Subscription | null => {
    logger.debug('Job completion tracked via polling for job ID:', id);
    return null;
  };

  const checkAvailableAgents = async (): Promise<AgentInfo[]> => {
    try {
      const response = await client.graphql({ query: listAvailableAgents });
      const agents = (response.data?.listAvailableAgents || []) as AgentInfo[];
      setAvailableAgents(agents);
      logger.debug('Available agents:', agents);
      return agents;
    } catch (err) {
      logger.error('Error fetching available agents:', err);
      return [];
    }
  };

  const submitTroubleshootQuery = async (): Promise<void> => {
    try {
      setIsSubmitting(true);
      setJobResult(null);
      setAgentMessages(null);
      setError(null);

      if (subscription) {
        subscription.unsubscribe();
      }

      // Check if Error-Analyzer-Agent agent exists
      const agents = await checkAvailableAgents();
      const errorAnalyzer = agents.find((agent) => agent.agent_id === 'Error-Analyzer-Agent');

      if (!errorAnalyzer) {
        throw new Error(`Error-Analyzer-Agent agent is not available. Available agents: ${agents.map((a) => a.agent_id).join(', ')}`);
      }

      logger.debug('Submitting troubleshoot query for document:', documentItem?.objectKey);
      logger.debug('Query:', query);
      logger.debug('Agent IDs:', ['Error-Analyzer-Agent']);

      const response = await client.graphql({
        query: submitAgentQuery,
        variables: {
          query,
          agentIds: ['Error-Analyzer-Agent'],
        },
      });

      logger.debug('Submit response:', response);

      const job = response.data?.submitAgentQuery;
      logger.debug('Job created:', job);

      if (!job) {
        throw new Error('Failed to create troubleshoot job');
      }

      setJobId(job.jobId);
      setJobStatus(job.status);

      subscribeToJobCompletion(job.jobId);
    } catch (err) {
      logger.error('Error submitting troubleshoot query:', err);
      setError((err as Error).message || { message: messages.submitFailed });
      setJobStatus('FAILED');
    } finally {
      setIsSubmitting(false);
    }
  };

  // On open: resume a job still running, show the last result of one that finished
  // (each run is a paid agent job, so a new one starts only on Run again), or start one.
  useEffect(() => {
    if (visible) {
      setMinimized(false); // always reopen expanded
      if (existingJob && ['PENDING', 'PROCESSING', 'COMPLETED', 'FAILED'].includes(existingJob.status)) {
        logger.info('Reopening troubleshoot job:', existingJob.jobId, existingJob.status);
        setJobId(existingJob.jobId);
        setJobStatus(existingJob.status);
        setJobResult(existingJob.result ?? null);
        setAgentMessages(existingJob.agentMessages);
        setError(existingJob.error ?? null);
        subscribeToJobCompletion(existingJob.jobId);
      } else {
        logger.info('Starting new troubleshoot job for document:', documentItem?.objectKey);
        submitTroubleshootQuery();
      }
    }
  }, [visible]);

  // Poll for job status as fallback
  useEffect(() => {
    let intervalId: ReturnType<typeof setInterval> | undefined;

    if (jobId && jobStatus && (jobStatus === 'PENDING' || jobStatus === 'PROCESSING')) {
      intervalId = setInterval(async () => {
        try {
          logger.debug('Polling job status for job ID:', jobId);
          const response = await client.graphql({
            query: getAgentJobStatus,
            variables: { jobId },
          });

          const job = response.data?.getAgentJobStatus;
          logger.debug('Polled job status:', job);

          if (job) {
            setAgentMessages(job.agent_messages);

            if (job.status !== jobStatus) {
              setJobStatus(job.status);

              if (job.status === 'COMPLETED') {
                setJobResult(job.result ?? null);
                clearInterval(intervalId);
              } else if (job.status === 'FAILED') {
                setError(job.error ?? { message: messages.jobFailed });
                clearInterval(intervalId);
              }
            }
          }
        } catch (err) {
          logger.error('Error polling job status:', err);
        }
      }, 2000); // Poll every 2 seconds
    }

    return () => {
      if (intervalId) {
        clearInterval(intervalId);
      }
    };
  }, [jobId, jobStatus]);

  // Cleanup subscription on unmount
  useEffect(() => {
    return () => {
      if (subscription) {
        subscription.unsubscribe();
      }
    };
  }, [subscription]);

  // Update parent component when job state changes
  useEffect(() => {
    if (jobId && onJobUpdate) {
      onJobUpdate({
        jobId,
        status: jobStatus,
        result: jobResult,
        agentMessages,
        error,
        timestamp: Date.now(),
        documentKey: documentItem?.objectKey,
      });
    }
  }, [jobId, jobStatus, jobResult, agentMessages, error]);

  // Clean up subscription when modal closes (but preserve job state)
  useEffect(() => {
    if (!visible && subscription) {
      subscription.unsubscribe();
      setSubscription(null);
    }
  }, [visible]);

  // Live status for the minimized chip.
  const isRunning = jobStatus === 'PENDING' || jobStatus === 'PROCESSING' || isSubmitting;

  // When minimized, collapse to a restore chip. The polling/subscription
  // effects keep running because `visible` stays true, so the analysis
  // continues in the background and its result is ready on restore.
  if (visible && minimized) {
    return (
      <div className="troubleshoot-restore-chip">
        <Button
          iconName={isRunning ? undefined : 'status-positive'}
          onClick={() => setMinimized(false)}
          ariaLabel={intl.formatMessage(messages.restoreAriaLabel)}
        >
          {isRunning ? (
            <>
              <Spinner /> <FormattedMessage {...messages.running} />
            </>
          ) : (
            <FormattedMessage {...messages.ready} />
          )}
        </Button>
      </div>
    );
  }

  return (
    <Modal
      // Only the Close button dismisses. Ignore overlay-click / Esc so an
      // in-progress analysis (which can take ~30s) is never torn down by an
      // accidental click outside the window.
      onDismiss={({ detail }) => {
        if (detail.reason === 'closeButton') onDismiss();
      }}
      visible={visible}
      size="large"
      header={
        <Header variant="h1">
          <FormattedMessage {...messages.title} />
        </Header>
      }
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            {canReport && (
              <Button iconName="copy" onClick={handleCopyFullDetails}>
                <FormattedMessage {...(copied ? messages.copied : messages.copy)} />
              </Button>
            )}
            {canReport && (
              <Button iconName="bug" href={reportIssueUrl} target="_blank" rel="noopener noreferrer">
                <FormattedMessage {...messages.report} />
              </Button>
            )}
            {isTerminal && (
              <Button iconName="refresh" loading={isSubmitting} onClick={() => void submitTroubleshootQuery()}>
                <FormattedMessage {...messages.runAgain} />
              </Button>
            )}
            <Button iconName="treeview-collapse" onClick={() => setMinimized(true)}>
              <FormattedMessage {...messages.minimize} />
            </Button>
            <Button variant="primary" onClick={onDismiss}>
              <FormattedMessage {...messages.close} />
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="l">
        <Alert type="info">
          <FormattedMessage {...messages.analyzing} values={{ objectKey: documentItem?.objectKey, strong }} />
        </Alert>

        {isSubmitting && (
          <Box textAlign="center" padding={{ vertical: 'l' }}>
            <Spinner size="large" />
            <Box padding={{ top: 's' }}>
              <FormattedMessage {...messages.analyzingFailure} />
            </Box>
          </Box>
        )}

        {shownError && <Alert type="error">{shownError}</Alert>}

        {jobStatus && jobStatus !== 'FAILED' && (
          <Alert type={jobStatus === 'COMPLETED' ? 'success' : 'info'}>
            <FormattedMessage {...messages.status} values={{ status: jobStatus }} />
          </Alert>
        )}

        {jobResult && <AgentResultDisplay result={jobResult} query={query} />}

        {(agentMessages || jobStatus === 'PROCESSING') && (
          <AgentMessagesDisplay agentMessages={agentMessages as string} isProcessing={jobStatus === 'PROCESSING'} />
        )}
      </SpaceBetween>
    </Modal>
  );
};

export default TroubleshootModal;
