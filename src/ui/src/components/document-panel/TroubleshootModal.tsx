// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React, { useState, useEffect } from 'react';
import { generateClient } from '../../api/client-shim';
import { ConsoleLogger } from 'aws-amplify/utils';
import { Modal, Box, SpaceBetween, Button, Spinner, Alert, Header } from '@cloudscape-design/components';

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

interface ExistingJob {
  jobId: string;
  status: string;
  result?: string | Record<string, unknown>;
  agentMessages?: unknown[] | Record<string, unknown>;
  error?: string;
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
        error: string | null;
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
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [subscription, setSubscription] = useState<Subscription | null>(null);
  const [_availableAgents, setAvailableAgents] = useState<AgentInfo[]>([]);
  const [copied, setCopied] = useState(false);
  // When minimized the modal collapses to a small restore chip so the
  // analysis keeps running (and polling continues) instead of being torn down.
  const [minimized, setMinimized] = useState(false);

  const deploymentContext = useDeploymentContext();

  const query = `Troubleshoot ${documentItem?.objectKey} for failures or performance issues.`;

  // A terminal (COMPLETED/FAILED) job with either findings or an error is worth
  // reporting. Build the document context once so the "Report this issue"
  // button and "Copy full details" share the same data.
  const isTerminal = jobStatus === 'COMPLETED' || jobStatus === 'FAILED';
  const findings = extractFindingsText(jobResult);
  const canReport = isTerminal && (Boolean(findings) || Boolean(error));

  const docContext: DocumentContext = {
    objectKey: documentItem?.objectKey,
    objectStatus: documentItem?.objectStatus,
    configVersion: documentItem?.configVersion as string | undefined,
    executionArn: documentItem?.executionArn as string | undefined,
    jobError: error ?? undefined,
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
      setError((err as Error).message || 'Failed to submit troubleshoot query');
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
                setError(job.error ?? 'Job processing failed');
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
          ariaLabel="Restore Troubleshoot window"
        >
          {isRunning ? (
            <>
              <Spinner /> Troubleshooting…
            </>
          ) : (
            'Troubleshoot results ready'
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
      header={<Header variant="h1">Troubleshoot Document</Header>}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            {canReport && (
              <Button iconName="copy" onClick={handleCopyFullDetails}>
                {copied ? 'Copied' : 'Copy full details'}
              </Button>
            )}
            {canReport && (
              <Button iconName="bug" href={reportIssueUrl} target="_blank" rel="noopener noreferrer">
                Report this issue on GitHub
              </Button>
            )}
            {isTerminal && (
              <Button iconName="refresh" loading={isSubmitting} onClick={() => void submitTroubleshootQuery()}>
                Run again
              </Button>
            )}
            <Button iconName="treeview-collapse" onClick={() => setMinimized(true)}>
              Minimize
            </Button>
            <Button variant="primary" onClick={onDismiss}>
              Close
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="l">
        <Alert type="info">
          Analyzing document: <strong>{documentItem?.objectKey}</strong>
        </Alert>

        {isSubmitting && (
          <Box textAlign="center" padding={{ vertical: 'l' }}>
            <Spinner size="large" />
            <Box padding={{ top: 's' }}>Analyzing document failure...</Box>
          </Box>
        )}

        {error && <Alert type="error">{error}</Alert>}

        {jobStatus && jobStatus !== 'FAILED' && <Alert type={jobStatus === 'COMPLETED' ? 'success' : 'info'}>Status: {jobStatus}</Alert>}

        {jobResult && <AgentResultDisplay result={jobResult} query={query} />}

        {(agentMessages || jobStatus === 'PROCESSING') && (
          <AgentMessagesDisplay agentMessages={agentMessages as string} isProcessing={jobStatus === 'PROCESSING'} />
        )}
      </SpaceBetween>
    </Modal>
  );
};

export default TroubleshootModal;
