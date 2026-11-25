// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import React, { useEffect } from 'react';
import { generateClient } from '../../api/client-shim';
import { ConsoleLogger } from 'aws-amplify/utils';
import { Container, Header, SpaceBetween, Spinner, Box } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, useIntl, type IntlShape, type NoMessageValues } from 'react-intl';

import { submitAgentQuery, getAgentJobStatus, onAgentJobComplete } from '../../graphql/generated';
import { useAnalyticsContext } from '../../contexts/analytics';
import type { AnalyticsError } from '../../types/analytics';

import AgentQueryInput from './AgentQueryInput';
import AgentJobStatus from './AgentJobStatus';
import AgentResultDisplay from './AgentResultDisplay';
import AgentMessagesDisplay from './AgentMessagesDisplay';

const client = generateClient();

const logger = new ConsoleLogger('DocumentsAgentsLayout');

const messages = defineMessages<{
  title: NoMessageValues;
  submitting: NoMessageValues;
  jobFailed: NoMessageValues;
  fetchAfterCompletionFailed: NoMessageValues;
  fetchFailed: { readonly error: string };
  invalidNotification: NoMessageValues;
  subscriptionError: { readonly error: string };
  subscriptionSetupFailed: { readonly error: string };
  submitFailed: NoMessageValues;
  unknownError: NoMessageValues;
}>({
  title: {
    id: 'agents.page.title',
    defaultMessage: 'Agent Analysis',
    description: 'Heading of the agents page, where the user puts a question about the documents to the selected agents.',
  },
  submitting: {
    id: 'agents.page.submitting',
    defaultMessage: 'Submitting your query...',
    description: 'Shown under a spinner on the agents page while the question is being sent.',
  },
  jobFailed: {
    id: 'agents.page.jobFailed',
    defaultMessage: 'Job processing failed',
    description: "Error on the agents page when the agents' job failed and the server sent no reason.",
  },
  fetchAfterCompletionFailed: {
    id: 'agents.page.fetchAfterCompletionFailed',
    defaultMessage: 'Failed to fetch job details after completion',
    description: "Error on the agents page when the agents' job finished but its result could not be read.",
  },
  fetchFailed: {
    id: 'agents.page.fetchFailed',
    defaultMessage: 'Failed to fetch job details: {error}',
    description:
      "Error on the agents page when reading the finished job's result failed. {error} is the error's own text, as it arrives, or the unknown-error text.",
  },
  invalidNotification: {
    id: 'agents.page.invalidNotification',
    defaultMessage: 'Received invalid completion notification. Check console logs for details.',
    description: "Error on the agents page when the notice that the agents' job finished could not be read.",
  },
  subscriptionError: {
    id: 'agents.page.subscriptionError',
    defaultMessage: 'Subscription error: {error}',
    description:
      "Error on the agents page when following the agents' job failed. {error} is the error's own text, as it arrives, or the unknown-error text.",
  },
  subscriptionSetupFailed: {
    id: 'agents.page.subscriptionSetupFailed',
    defaultMessage: 'Failed to set up job status subscription: {error}',
    description:
      "Error on the agents page when following the agents' job could not start. {error} is the error's own text, as it arrives, or the unknown-error text.",
  },
  submitFailed: {
    id: 'agents.page.submitFailed',
    defaultMessage: 'Failed to submit query',
    description: 'Error on the agents page when the question could not be sent and the server sent no reason.',
  },
  unknownError: {
    id: 'agents.page.unknownError',
    defaultMessage: 'Unknown error',
    description: "Shown in the errors of the agents page in place of an error's own text when the error has none.",
  },
});

/** The page's error in the UI language. A thrown error without text of its own reads as the unknown-error text. */
const errorText = (error: AnalyticsError | null, intl: IntlShape): string | null => {
  if (error === null || typeof error === 'string') return error;
  if ('detail' in error) return intl.formatMessage(error.message, { error: error.detail || intl.formatMessage(messages.unknownError) });
  return intl.formatMessage(error.message);
};

const DocumentsAgentsLayout = (): React.JSX.Element => {
  const intl = useIntl();
  const { analyticsState, updateAnalyticsState } = useAnalyticsContext();
  const { queryText, jobId, jobStatus, jobResult, agentMessages, error, isSubmitting, subscription } = analyticsState;

  const subscribeToJobCompletion = (id: string) => {
    try {
      logger.debug('Subscribing to job completion for job ID:', id);
      const sub = (
        client.graphql({
          query: onAgentJobComplete,
          variables: { jobId: id },
        }) as unknown as { subscribe: (callbacks: Record<string, unknown>) => { unsubscribe: () => void } }
      ).subscribe({
        next: async (subscriptionData: Record<string, unknown>) => {
          const data = (subscriptionData as Record<string, unknown>)?.data as Record<string, unknown> | undefined;
          const jobCompleted = data?.onAgentJobComplete as Record<string, unknown> | undefined;
          logger.debug('Job completion notification:', jobCompleted);

          if (jobCompleted) {
            // Job completed, now fetch the actual job details
            try {
              logger.debug('Fetching job details after completion notification');
              const jobResponse = await client.graphql({
                query: getAgentJobStatus,
                variables: { jobId: id },
              });

              const jobResponseData = (jobResponse as unknown as Record<string, unknown>)?.data as Record<string, unknown> | undefined;
              const job = jobResponseData?.getAgentJobStatus as Record<string, unknown> | undefined;
              logger.debug('Fetched job details:', job);

              if (job) {
                updateAnalyticsState({
                  jobStatus: job.status as string,
                  agentMessages: job.agent_messages as string,
                });

                if (job.status === 'COMPLETED') {
                  updateAnalyticsState({ jobResult: job.result as string });
                } else if (job.status === 'FAILED') {
                  updateAnalyticsState({ error: (job.error as string) || { message: messages.jobFailed } });
                }
              } else {
                logger.error('Failed to fetch job details after completion notification');
                updateAnalyticsState({ error: { message: messages.fetchAfterCompletionFailed } });
              }
            } catch (fetchError) {
              logger.error('Error fetching job details:', fetchError);
              updateAnalyticsState({ error: { message: messages.fetchFailed, detail: (fetchError as Error).message } });
            }
          } else {
            logger.error('Received invalid completion notification. Full response:', JSON.stringify(subscriptionData, null, 2));
            updateAnalyticsState({
              error: { message: messages.invalidNotification },
            });
          }
        },
        error: (err: Record<string, unknown>) => {
          logger.error('Subscription error:', err);
          logger.error('Error details:', JSON.stringify(err, null, 2));
          updateAnalyticsState({ error: { message: messages.subscriptionError, detail: (err as unknown as Error).message } });
        },
      });

      updateAnalyticsState({ subscription: sub });
      return sub;
    } catch (err) {
      logger.error('Error setting up subscription:', err);
      updateAnalyticsState({ error: { message: messages.subscriptionSetupFailed, detail: (err as Error).message } });
      return null;
    }
  };

  // Clean up subscription when component unmounts or when jobId changes
  useEffect(() => {
    return () => {
      if (subscription) {
        logger.debug('Cleaning up subscription');
        (subscription as { unsubscribe: () => void }).unsubscribe();
      }
    };
  }, [subscription]);

  const handleSubmitQuery = async (query: string, agentIds: string | string[], existingJobId: string | null = null) => {
    try {
      updateAnalyticsState({
        queryText: query,
        currentInputText: query, // Also update the input text to match the submitted query
      });

      // If an existing job ID is provided, fetch that job's result instead of creating a new job
      if (existingJobId) {
        logger.debug('Using existing job:', existingJobId);
        updateAnalyticsState({ jobId: existingJobId });

        // Fetch the job status and result
        const response = await client.graphql({
          query: getAgentJobStatus,
          variables: { jobId: existingJobId },
        });

        const responseData = (response as unknown as Record<string, unknown>)?.data as Record<string, unknown> | undefined;
        const job = responseData?.getAgentJobStatus as Record<string, unknown> | undefined;
        if (job) {
          updateAnalyticsState({
            jobStatus: job.status as string,
            agentMessages: job.agent_messages as string,
          });
          if (job.status === 'COMPLETED') {
            updateAnalyticsState({ jobResult: job.result as string });
          } else if (job.status === 'FAILED') {
            updateAnalyticsState({ error: (job.error as string) || { message: messages.jobFailed } });
          } else {
            // If job is still processing, subscribe to updates
            subscribeToJobCompletion(existingJobId);
          }
        }
        return;
      }

      // Otherwise, create a new job
      updateAnalyticsState({
        isSubmitting: true,
        jobResult: null,
        agentMessages: null,
        error: null,
      });

      // Clean up previous subscription if exists
      if (subscription) {
        (subscription as { unsubscribe: () => void }).unsubscribe();
      }

      logger.debug('Submitting agent query:', query, 'with agents:', agentIds);
      const response = await client.graphql({
        query: submitAgentQuery,
        variables: { query, agentIds: Array.isArray(agentIds) ? agentIds : [agentIds] },
      });

      const responseData = (response as unknown as Record<string, unknown>)?.data as Record<string, unknown> | undefined;
      const job = responseData?.submitAgentQuery as Record<string, unknown> | undefined;
      logger.debug('Job created:', job);

      if (!job) {
        throw new Error('Failed to create analytics job - received null response');
      }

      updateAnalyticsState({
        jobId: job.jobId as string,
        jobStatus: job.status as string,
      });

      // Subscribe to job completion
      subscribeToJobCompletion(job.jobId as string);

      // Add immediate poll after 1 second for quick feedback
      setTimeout(async () => {
        try {
          logger.debug('Immediate poll for job ID:', job.jobId);
          const pollResponse = await client.graphql({
            query: getAgentJobStatus,
            variables: { jobId: job.jobId as string },
          });

          const pollResponseData = (pollResponse as unknown as Record<string, unknown>)?.data as Record<string, unknown> | undefined;
          const polledJob = pollResponseData?.getAgentJobStatus as Record<string, unknown> | undefined;
          logger.debug('Immediate poll result:', polledJob);

          if (polledJob && polledJob.status !== job.status) {
            updateAnalyticsState({
              jobStatus: polledJob.status as string,
              agentMessages: polledJob.agent_messages as string,
            });

            if (polledJob.status === 'COMPLETED') {
              updateAnalyticsState({ jobResult: polledJob.result as string });
            } else if (polledJob.status === 'FAILED') {
              updateAnalyticsState({ error: (polledJob.error as string) || { message: messages.jobFailed } });
            }
          }
        } catch (pollErr) {
          logger.debug('Immediate poll failed (non-critical):', pollErr);
          // Don't set error for immediate poll failures as regular polling will continue
        }
      }, 1000);
    } catch (err) {
      logger.error('Error submitting query:', err);
      logger.error('Error structure:', JSON.stringify(err, null, 2));

      let errorMessage: AnalyticsError = { message: messages.submitFailed };

      const typedErr = err as Record<string, unknown>;
      // Extract error message from GraphQL error structure
      if (
        typedErr.errors &&
        (typedErr.errors as Array<Record<string, unknown>>).length > 0 &&
        (typedErr.errors as Array<Record<string, unknown>>)[0].message
      ) {
        errorMessage = (typedErr.errors as Array<Record<string, unknown>>)[0].message as string;
      } else if (typedErr.message) {
        errorMessage = typedErr.message as string;
      } else if (typedErr.data && (typedErr.data as Record<string, unknown>).errors) {
        const dataErrors = (typedErr.data as Record<string, unknown>).errors as Array<Record<string, unknown>>;
        if (dataErrors.length > 0 && dataErrors[0].message) {
          errorMessage = dataErrors[0].message as string;
        }
      } else if (typeof err === 'string') {
        errorMessage = err;
      }

      updateAnalyticsState({
        error: errorMessage,
        jobStatus: 'FAILED',
      });
    } finally {
      updateAnalyticsState({ isSubmitting: false });
    }
  };

  // Poll for job status as a fallback in case subscription fails
  useEffect(() => {
    let intervalId: ReturnType<typeof setInterval>;

    if (jobId && jobStatus && (jobStatus === 'PENDING' || jobStatus === 'PROCESSING')) {
      intervalId = setInterval(async () => {
        try {
          logger.debug('Polling job status for job ID:', jobId);
          const response = await client.graphql({
            query: getAgentJobStatus,
            variables: { jobId },
          });

          const responseData = (response as unknown as Record<string, unknown>)?.data as Record<string, unknown> | undefined;
          const job = responseData?.getAgentJobStatus as Record<string, unknown> | undefined;
          logger.debug('Polled job status:', job);

          if (job) {
            // Always update agent messages, even if status hasn't changed
            updateAnalyticsState({ agentMessages: job.agent_messages as string });

            if (job.status !== jobStatus) {
              updateAnalyticsState({ jobStatus: job.status as string });

              if (job.status === 'COMPLETED') {
                updateAnalyticsState({ jobResult: job.result as string });
                clearInterval(intervalId);
              } else if (job.status === 'FAILED') {
                updateAnalyticsState({ error: (job.error as string) || { message: messages.jobFailed } });
                clearInterval(intervalId);
              }
            }
          }
        } catch (err) {
          logger.error('Error polling job status:', err);
          // Don't set error here to avoid overriding subscription errors
        }
      }, 1000); // Poll every 1 second
    }

    return () => {
      if (intervalId) {
        clearInterval(intervalId);
      }
    };
  }, [jobId, jobStatus, updateAnalyticsState]);

  return (
    <Container
      header={
        <Header variant="h2">
          <FormattedMessage {...messages.title} />
        </Header>
      }
    >
      <SpaceBetween size="l">
        <AgentQueryInput onSubmit={handleSubmitQuery} isSubmitting={isSubmitting} selectedResult={null} />

        {isSubmitting && (
          <Box textAlign="center" padding={{ vertical: 'l' }}>
            <Spinner size="large" />
            <Box padding={{ top: 's' }}>
              <FormattedMessage {...messages.submitting} />
            </Box>
          </Box>
        )}

        <AgentJobStatus jobId={jobId} status={jobStatus} error={errorText(error, intl)} />

        {jobResult != null && <AgentResultDisplay result={jobResult as string | Record<string, unknown>} query={queryText as string} />}

        {/* Show agent messages at the bottom when available */}
        {(agentMessages || jobStatus === 'PROCESSING') && (
          <AgentMessagesDisplay agentMessages={agentMessages as string} isProcessing={jobStatus === 'PROCESSING'} />
        )}
      </SpaceBetween>
    </Container>
  );
};

export default DocumentsAgentsLayout;
