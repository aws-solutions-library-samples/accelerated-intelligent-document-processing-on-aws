// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import React, { useState, useEffect, useRef } from 'react';
import { generateClient } from '../../api/client-shim';
import { ConsoleLogger } from 'aws-amplify/utils';
import {
  FormField,
  Textarea,
  Button,
  Grid,
  Box,
  SpaceBetween,
  ButtonDropdown,
  Checkbox,
  Modal,
  Header,
  Link,
} from '@cloudscape-design/components';
import type { ButtonDropdownProps } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, useIntl, type MessageTag, type NoMessageValues } from 'react-intl';

import { listAgentJobs, deleteAgentJob, listAvailableAgents } from '../../graphql/generated';
import { useAnalyticsContext } from '../../contexts/analytics';
import { useFormatters } from '../../i18n/format';

interface AvailableAgent {
  agent_id: string;
  agent_name: string;
  agent_description?: string;
  sample_queries?: string[];
}

interface AgentJob {
  jobId: string;
  query: string;
  status: string;
  createdAt?: string;
  agentIds?: string;
  [key: string]: unknown;
}

interface SelectedOption {
  value: string;
  label: string;
}

interface SelectedResult {
  query: string;
  jobId?: string;
}

interface AgentQueryInputProps {
  onSubmit: (query: string, agents: string[], jobId?: string) => void;
  isSubmitting?: boolean;
  selectedResult?: SelectedResult | null;
}

const client = generateClient();

// Custom styles for expandable textarea
// ALL_CAPS on purpose: the string-debt counter skips ALL_CAPS declarations, and this CSS is not UI text.
const TEXTAREA_STYLES = `
  .expandable-textarea {
    max-height: 250px;
    overflow-y: auto !important;
    resize: vertical;
  }
`;

const logger = new ConsoleLogger('AgentQueryInput');

const messages = defineMessages<{
  unknownDate: NoMessageValues;
  noHistory: NoMessageValues;
  noQuery: NoMessageValues;
  completed: NoMessageValues;
  unknownStatus: NoMessageValues;
  deleteAriaLabel: { readonly query: string };
  agentsHeading: NoMessageValues;
  mcpButton: NoMessageValues;
  loadingAgents: NoMessageValues;
  noAgents: NoMessageValues;
  sampleQueries: NoMessageValues;
  selectAll: NoMessageValues;
  deselectAll: NoMessageValues;
  questionLabel: NoMessageValues;
  placeholder: NoMessageValues;
  placeholderNoAgent: NoMessageValues;
  submit: NoMessageValues;
  submitting: NoMessageValues;
  clear: NoMessageValues;
  historyButton: NoMessageValues;
  historySelected: NoMessageValues;
  mcpTitle: NoMessageValues;
  mcpClose: NoMessageValues;
  mcpWhatTitle: NoMessageValues;
  mcpWhatBody: NoMessageValues;
  mcpAddingTitle: NoMessageValues;
  mcpAddingBody: NoMessageValues;
  mcpLearnMoreTitle: NoMessageValues;
  mcpLearnMoreBody: { readonly link: MessageTag };
}>({
  unknownDate: {
    id: 'agents.queryInput.unknownDate',
    defaultMessage: 'Unknown date',
    description: 'Shown in place of the date of a previous question on the agents page when the date cannot be read.',
  },
  noHistory: {
    id: 'agents.queryInput.noHistory',
    defaultMessage: 'No previous questions found',
    description: 'The only entry of the previous-questions list on the agents page when the user has asked none.',
  },
  noQuery: {
    id: 'agents.queryInput.noQuery',
    defaultMessage: 'No query',
    description: 'Shown in place of a previous question on the agents page when the question has no text.',
  },
  completed: {
    id: 'agents.queryInput.completed',
    defaultMessage: 'Completed',
    description: "Status after the date of a previous question on the agents page when the agents' job finished.",
  },
  unknownStatus: {
    id: 'agents.queryInput.unknownStatus',
    defaultMessage: 'Unknown status',
    description: "Status after the date of a previous question on the agents page when the job's status is missing.",
  },
  deleteAriaLabel: {
    id: 'agents.queryInput.deleteAriaLabel',
    defaultMessage: 'Delete query: {query}',
    description:
      'Screen-reader label of the button that deletes a previous question on the agents page. {query} is the question, as typed, cut to its first 50 characters.',
  },
  agentsHeading: {
    id: 'agents.queryInput.agentsHeading',
    defaultMessage: 'Select from available agents',
    description: 'Heading above the list of agents the user can put the question to, on the agents page.',
  },
  mcpButton: {
    id: 'agents.queryInput.mcpButton',
    defaultMessage: 'NEW: Integrate your own systems with MCP!',
    description: 'Button on the agents page that explains custom MCP agents. A rocket emoji is shown before it.',
  },
  loadingAgents: {
    id: 'agents.queryInput.loadingAgents',
    defaultMessage: 'Loading agents...',
    description: 'Shown in the list of agents on the agents page while the list loads.',
  },
  noAgents: {
    id: 'agents.queryInput.noAgents',
    defaultMessage: 'No agents available',
    description: 'Shown in the list of agents on the agents page when the deployment has no agents.',
  },
  sampleQueries: {
    id: 'agents.queryInput.sampleQueries',
    defaultMessage: 'Sample Queries:',
    description: "Heading above an agent's sample questions, in the tooltip of an agent on the agents page.",
  },
  selectAll: {
    id: 'agents.queryInput.selectAll',
    defaultMessage: 'Select All Agents',
    description: 'Button under the list of agents on the agents page that selects every agent.',
  },
  deselectAll: {
    id: 'agents.queryInput.deselectAll',
    defaultMessage: 'Deselect All Agents',
    description: 'Button under the list of agents on the agents page that clears the selection, shown when every agent is selected.',
  },
  questionLabel: {
    id: 'agents.queryInput.questionLabel',
    defaultMessage: 'Enter your question for the agent',
    description: 'Label of the text box in which the user types the question for the agents.',
  },
  placeholder: {
    id: 'agents.queryInput.placeholder',
    defaultMessage: 'Enter your question here...',
    description: 'Placeholder of the question box on the agents page when the first selected agent has no sample question to show instead.',
  },
  placeholderNoAgent: {
    id: 'agents.queryInput.placeholderNoAgent',
    defaultMessage: 'Select at least one available agent to get started',
    description: 'Placeholder of the question box on the agents page while no agent is selected.',
  },
  submit: {
    id: 'agents.queryInput.submit',
    defaultMessage: 'Submit question',
    description: 'Button that sends the question to the selected agents.',
  },
  submitting: {
    id: 'agents.queryInput.submitting',
    defaultMessage: 'Submitting...',
    description: 'Label of the submit button on the agents page while the question is being sent.',
  },
  clear: {
    id: 'agents.queryInput.clear',
    defaultMessage: 'Clear question',
    description: 'Button that clears the question and the results on the agents page.',
  },
  historyButton: {
    id: 'agents.queryInput.historyButton',
    defaultMessage: 'Select a previous question',
    description: 'Button that opens the list of previous questions on the agents page.',
  },
  historySelected: {
    id: 'agents.queryInput.historySelected',
    defaultMessage: 'Selected question',
    description: 'Label of the previous-questions button on the agents page when the selected question has no text to show.',
  },
  mcpTitle: {
    id: 'agents.queryInput.mcp.title',
    defaultMessage: 'Custom MCP Agents',
    description: 'Title of the dialog on the agents page that explains custom MCP agents.',
  },
  mcpClose: {
    id: 'agents.queryInput.mcp.close',
    defaultMessage: 'Close',
    description: 'Button that closes the custom MCP agents dialog.',
  },
  mcpWhatTitle: {
    id: 'agents.queryInput.mcp.whatTitle',
    defaultMessage: 'What are MCP Agents?',
    description: 'Heading in the custom MCP agents dialog.',
  },
  mcpWhatBody: {
    id: 'agents.queryInput.mcp.whatBody',
    defaultMessage:
      'Model Context Protocol (MCP) agents allow you to connect external tools and services to extend the capabilities of your document analysis workflow.',
    description: 'Paragraph in the custom MCP agents dialog, under "What are MCP Agents?".',
  },
  mcpAddingTitle: {
    id: 'agents.queryInput.mcp.addingTitle',
    defaultMessage: 'Adding Custom Agents',
    description: 'Heading in the custom MCP agents dialog.',
  },
  mcpAddingBody: {
    id: 'agents.queryInput.mcp.addingBody',
    defaultMessage:
      "You can add your own MCP agents by configuring external MCP servers in AWS Secrets Manager. This allows you to integrate custom tools, APIs, and services specific to your organization's needs without any code changes or redeployments.",
    description: 'Paragraph in the custom MCP agents dialog, under "Adding Custom Agents". AWS Secrets Manager is a product name.',
  },
  mcpLearnMoreTitle: {
    id: 'agents.queryInput.mcp.learnMoreTitle',
    defaultMessage: 'Learn More',
    description: 'Heading in the custom MCP agents dialog, above the link to the documentation.',
  },
  mcpLearnMoreBody: {
    id: 'agents.queryInput.mcp.learnMoreBody',
    defaultMessage: 'For detailed setup instructions and examples, see the <link>Custom MCP Agent Documentation</link>',
    description: 'Last paragraph of the custom MCP agents dialog. The text inside <link> links to the custom MCP agent documentation.',
  },
});

/** Renders a `<link>...</link>` element value: the custom MCP agent documentation. */
const mcpDocsLink = (chunks: React.ReactNode[]): React.JSX.Element => (
  <Link
    external
    href="https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/main/docs/custom-MCP-agent.md"
  >
    {chunks}
  </Link>
);

const AgentQueryInput = ({ onSubmit, isSubmitting = false, selectedResult = null }: AgentQueryInputProps): React.JSX.Element => {
  const intl = useIntl();
  const { analyticsState, updateAnalyticsState, resetAnalyticsState } = useAnalyticsContext();
  const { currentInputText } = analyticsState;
  const { formatDateTime } = useFormatters();

  const [queryHistory, setQueryHistory] = useState<AgentJob[]>([]);
  const [isLoadingHistory, setIsLoadingHistory] = useState(false);
  const [selectedOption, setSelectedOption] = useState<SelectedOption | null>(null);
  const [isDeletingJob, setIsDeletingJob] = useState(false);
  const [availableAgents, setAvailableAgents] = useState<AvailableAgent[]>([]);
  const [selectedAgents, setSelectedAgents] = useState<string[]>([]);
  const [isLoadingAgents, setIsLoadingAgents] = useState(false);
  const [showMcpInfoModal, setShowMcpInfoModal] = useState(false);
  const [hoveredAgent, setHoveredAgent] = useState<string | null>(null);
  const [hoverTimeout, setHoverTimeout] = useState<ReturnType<typeof setTimeout> | null>(null);
  const [mousePosition, setMousePosition] = useState({ x: 0, y: 0 });
  const lastFetchTimeRef = useRef(0);

  const handleMouseEnter = (agentId: string, event: React.MouseEvent): void => {
    setMousePosition({ x: event.clientX, y: event.clientY });

    // If a tooltip is already showing, switch instantly
    if (hoveredAgent) {
      setHoveredAgent(agentId);
      return;
    }

    // Otherwise, use the delay
    const timeout = setTimeout(() => {
      setHoveredAgent(agentId);
    }, 500);
    setHoverTimeout(timeout);
  };

  const handleMouseMove = (event: React.MouseEvent): void => {
    setMousePosition({ x: event.clientX, y: event.clientY });
  };

  const handleMouseLeave = (): void => {
    if (hoverTimeout) {
      clearTimeout(hoverTimeout);
      setHoverTimeout(null);
    }
    setHoveredAgent(null);
  };

  const handleAgentSelection = (agentId: string, isSelected: boolean): void => {
    if (isSelected) {
      setSelectedAgents((prev) => [...prev, agentId]);
    } else {
      setSelectedAgents((prev) => prev.filter((id) => id !== agentId));
    }
  };

  const handleSelectAllAgents = (e: CustomEvent | React.MouseEvent): void => {
    e.preventDefault();
    e.stopPropagation();
    const allSelected = selectedAgents.length === availableAgents.length;
    if (allSelected) {
      setSelectedAgents([]);
    } else {
      setSelectedAgents(availableAgents.map((agent) => agent.agent_id));
    }
  };

  const fetchAvailableAgents = async (): Promise<void> => {
    try {
      setIsLoadingAgents(true);
      const response = await client.graphql({
        query: listAvailableAgents,
      });

      const agents = (response as unknown as { data: { listAvailableAgents: AvailableAgent[] } })?.data?.listAvailableAgents || [];
      setAvailableAgents(agents);
    } catch (err) {
      logger.error('Error fetching available agents:', err);
      setAvailableAgents([]);
    } finally {
      setIsLoadingAgents(false);
    }
  };

  const fetchQueryHistory = async (force = false): Promise<void> => {
    // Don't fetch if we're already loading
    if (isLoadingHistory) return;

    // Don't fetch too frequently unless forced
    const now = Date.now();
    if (!force && now - lastFetchTimeRef.current < 5000) {
      // 5 second cooldown
      logger.debug('Skipping fetch due to cooldown');
      return;
    }

    try {
      setIsLoadingHistory(true);
      lastFetchTimeRef.current = now;

      let response: Record<string, unknown>;
      try {
        response = (await client.graphql({
          query: listAgentJobs,
          variables: { limit: 20 }, // Limit to most recent 20 queries
        })) as unknown as Record<string, unknown>;
      } catch (amplifyError: unknown) {
        // Amplify throws an exception when there are GraphQL errors, but the response might still contain valid data
        logger.warn('Amplify threw an exception due to GraphQL errors, checking for valid data:', amplifyError);

        const errRecord = amplifyError as Record<string, unknown>;
        // Check if the error object contains the actual response data
        if (errRecord.data && (errRecord.data as Record<string, unknown>).listAgentJobs) {
          logger.info('Found valid data in the error response, proceeding with processing');
          response = {
            data: errRecord.data,
            errors: (errRecord.errors as unknown[]) || [],
          };
        } else {
          // If there's no data in the error, re-throw to be handled by outer catch
          throw amplifyError;
        }
      }

      // Handle GraphQL errors gracefully - log them but continue processing valid data
      const errors = response.errors as unknown[] | undefined;
      if (errors && errors.length > 0) {
        logger.warn(`Received ${errors.length} GraphQL errors in listAgentJobs response:`, errors);
        logger.warn('Continuing to process valid data despite errors...');
      }

      // Get items array and filter out null values (corrupted items)
      const data = response?.data as Record<string, unknown> | undefined;
      const listResult = data?.listAgentJobs as { items?: unknown[] } | undefined;
      const rawItems = listResult?.items || [];
      const nonNullJobs = rawItems.filter((job): job is AgentJob => job !== null);

      logger.debug(`Raw response: ${rawItems.length} total items, ${nonNullJobs.length} non-null items`);
      logger.debug('Non-null jobs data:', nonNullJobs);

      // Filter out any jobs with invalid or missing required fields
      const validJobs = nonNullJobs.filter((job) => {
        try {
          // Check if job has required fields
          if (!job || !job.jobId || !job.query || !job.status) {
            logger.warn('Filtering out job with missing required fields:', job);
            return false;
          }

          // We'll keep jobs even with invalid dates - we'll handle them in the sort and display
          return true;
        } catch (e) {
          logger.warn(`Filtering out job with error: ${job?.jobId || 'unknown'}`, e);
          return false;
        }
      });

      logger.debug(`Filtered to ${validJobs.length} valid jobs`);

      // Sort by createdAt in descending order (newest first)
      // Use string comparison if date parsing fails
      const sortedJobs = [...validJobs].sort((a, b) => {
        try {
          // Try to parse dates and compare
          const dateA = a.createdAt ? new Date(a.createdAt) : new Date(0);
          const dateB = b.createdAt ? new Date(b.createdAt) : new Date(0);

          // Check if dates are valid
          if (Number.isNaN(dateA.getTime()) || Number.isNaN(dateB.getTime())) {
            // Fall back to string comparison if dates are invalid
            return (b.createdAt || '').localeCompare(a.createdAt || '');
          }

          return dateB.getTime() - dateA.getTime();
        } catch (e) {
          logger.warn('Error sorting jobs by date, using string comparison:', e);
          // Fall back to string comparison
          return (b.createdAt || '').localeCompare(a.createdAt || '');
        }
      });

      logger.debug('Final processed and sorted jobs:', sortedJobs);
      setQueryHistory(sortedJobs);

      // Log summary of what we processed
      if (errors && errors.length > 0) {
        logger.info(
          `Successfully processed ${sortedJobs.length} valid queries despite ${errors.length} GraphQL errors from corrupted items`,
        );
      } else {
        logger.info(`Successfully processed ${sortedJobs.length} queries with no errors`);
      }
    } catch (err) {
      logger.error('Error fetching query history:', err);
      // Only log as empty if it's a complete failure (network error, etc.)
      logger.error('Complete failure - setting empty history');
      setQueryHistory([]);
    } finally {
      setIsLoadingHistory(false);
    }
  };

  // Fetch query history and agents when component mounts
  useEffect(() => {
    fetchQueryHistory(true);
    fetchAvailableAgents();
  }, []);

  // Update query input when a result is selected externally
  useEffect(() => {
    if (selectedResult) {
      updateAnalyticsState({ currentInputText: selectedResult.query });
      setSelectedOption(null); // Reset dropdown selection
    }
  }, [selectedResult, updateAnalyticsState]);

  const handleSubmit = (e: React.FormEvent): void => {
    e.preventDefault();
    if (currentInputText.trim() && selectedAgents.length > 0 && !isSubmitting) {
      onSubmit(currentInputText, selectedAgents);
      setSelectedOption(null); // Reset dropdown selection after submission

      // Refresh the query history after a short delay to include the new query
      setTimeout(() => {
        fetchQueryHistory(true);
      }, 2000); // Wait 2 seconds to allow the backend to process the query
    }
  };

  const handleClearQuery = (): void => {
    // Clean up any existing subscription before resetting state
    if (analyticsState.subscription) {
      (analyticsState.subscription as { unsubscribe: () => void }).unsubscribe();
    }

    // Reset all analytics state to initial values
    resetAnalyticsState();
    // Also clear local component state
    setSelectedOption(null);
  };

  const handleDropdownItemClick = ({ detail }: { detail: { id: string } }): void => {
    console.log('Previous query clicked, detail:', detail);

    // Prevent dropdown item selection if a delete operation is in progress
    if (isDeletingJob) {
      console.log('Delete operation in progress, ignoring click');
      return;
    }

    const selectedJob = queryHistory.find((job) => job.jobId === detail.id);
    console.log('Selected job:', selectedJob);

    if (selectedJob) {
      updateAnalyticsState({
        currentInputText: selectedJob.query,
        error: null, // Clear any previous error
      });
      setSelectedOption({ value: selectedJob.jobId, label: selectedJob.query });

      let agentsToUse = selectedAgents;
      console.log('Current selected agents:', selectedAgents);

      // Auto-select agents from agentIds
      if (selectedJob.agentIds) {
        console.log('Job has agentIds:', selectedJob.agentIds);
        try {
          const agentIds = JSON.parse(selectedJob.agentIds) as string[];
          console.log('Parsed agentIds:', agentIds);

          if (agentIds.length > 0) {
            console.log('Setting selected agents to:', agentIds);
            setSelectedAgents(agentIds);
            agentsToUse = agentIds;
          }
        } catch (error) {
          console.error('Failed to parse agentIds from selected job:', error);
          logger.warn('Failed to parse agentIds from selected job:', error);
        }
      } else {
        console.log('Job has no agentIds');
      }

      console.log('Submitting with agents:', agentsToUse);
      // Submit the job to display its current status and results (if completed)
      // This will work for both completed jobs and in-progress jobs
      onSubmit(selectedJob.query, agentsToUse, selectedJob.jobId);
    }
  };

  // Format date for display in dropdown
  const formatDate = (dateString: string | undefined): string => {
    try {
      const date = new Date(dateString as string);
      // Check if date is valid
      if (Number.isNaN(date.getTime())) {
        return intl.formatMessage(messages.unknownDate);
      }
      return formatDateTime(date);
    } catch (e) {
      logger.warn(`Error formatting date: ${dateString}`, e);
      return intl.formatMessage(messages.unknownDate);
    }
  };

  // Create dropdown items with delete functionality
  const createDropdownItems = (): { id?: string; text: string | React.ReactNode; disabled: boolean }[] => {
    if (queryHistory.length === 0) {
      return [{ text: intl.formatMessage(messages.noHistory), disabled: true }];
    }

    return queryHistory.map((job) => {
      const displayText = job.query?.length > 50 ? `${job.query.substring(0, 50)}...` : job.query || intl.formatMessage(messages.noQuery);
      const dateText = formatDate(job.createdAt);

      return {
        id: job.jobId,
        text: (
          <div style={{ display: 'flex', alignItems: 'center', width: '100%', minHeight: '40px' }}>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontWeight: 'normal', marginBottom: '2px' }}>{displayText}</div>
              <div style={{ fontSize: '12px', color: '#5f6b7a' }}>
                {dateText} •{' '}
                {job.status === 'COMPLETED' ? (
                  <FormattedMessage {...messages.completed} />
                ) : (
                  job.status || <FormattedMessage {...messages.unknownStatus} />
                )}
              </div>
            </div>
            <Button
              variant="icon"
              iconName="remove"
              onClick={async (e) => {
                e.preventDefault();
                e.stopPropagation();

                // Set flag to prevent dropdown item selection
                setIsDeletingJob(true);

                try {
                  await client.graphql({
                    query: deleteAgentJob,
                    variables: {
                      jobId: job.jobId,
                    },
                  });

                  logger.debug('Successfully deleted job:', job.jobId);

                  // Remove the deleted job from the local state
                  setQueryHistory((prev) => prev.filter((historyJob) => historyJob.jobId !== job.jobId));

                  // If the deleted job was currently selected, clear the selection
                  if (selectedOption && selectedOption.value === job.jobId) {
                    setSelectedOption(null);
                    updateAnalyticsState({ currentInputText: '' });
                  }
                } catch (err) {
                  logger.error('Error deleting job:', err);
                } finally {
                  // Reset the flag after a short delay to ensure event handling is complete
                  setTimeout(() => {
                    setIsDeletingJob(false);
                  }, 100);
                }
              }}
              ariaLabel={intl.formatMessage(messages.deleteAriaLabel, { query: displayText })}
            />
          </div>
        ),
        disabled: false,
      };
    });
  };

  return (
    <>
      <style>{TEXTAREA_STYLES}</style>
      <form onSubmit={handleSubmit}>
        <SpaceBetween size="l">
          <Box>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <Box fontSize="heading-xs" fontWeight="bold">
                <FormattedMessage {...messages.agentsHeading} />
              </Box>
              <Button variant="normal" onClick={() => setShowMcpInfoModal(true)} {...({ fontSize: 'body-s' } as Record<string, unknown>)}>
                🚀 <FormattedMessage {...messages.mcpButton} />
              </Button>
            </div>
            <div
              style={{
                maxHeight: '200px',
                overflowY: 'auto',
                border: '1px solid #d5dbdb',
                padding: '8px',
                position: 'relative',
                marginTop: '16px',
              }}
            >
              {isLoadingAgents && (
                <Box textAlign="center" padding="m">
                  <FormattedMessage {...messages.loadingAgents} />
                </Box>
              )}
              {!isLoadingAgents && availableAgents.length === 0 && (
                <Box textAlign="center" padding="m">
                  <b>
                    <FormattedMessage {...messages.noAgents} />
                  </b>
                </Box>
              )}
              {!isLoadingAgents && availableAgents.length > 0 && (
                <SpaceBetween size="s">
                  {availableAgents.map((agent) => (
                    <div
                      key={agent.agent_id}
                      onMouseEnter={(e) => {
                        handleMouseEnter(agent.agent_id, e);
                        e.currentTarget.style.backgroundColor = '#f8f9fa';
                      }}
                      onMouseMove={handleMouseMove}
                      onMouseLeave={(e) => {
                        handleMouseLeave();
                        e.currentTarget.style.backgroundColor = 'transparent';
                      }}
                      style={{
                        width: '100%',
                        padding: '4px',
                        borderRadius: '4px',
                        transition: 'background-color 0.2s ease',
                      }}
                    >
                      <Checkbox
                        checked={selectedAgents.includes(agent.agent_id)}
                        onChange={({ detail }) => handleAgentSelection(agent.agent_id, detail.checked)}
                      >
                        <Box>
                          <Box fontWeight="bold">{agent.agent_name}</Box>
                          <Box fontSize="body-s" color="text-body-secondary">
                            {(agent.agent_description?.length ?? 0) > 150
                              ? `${agent.agent_description?.substring(0, 150)}...`
                              : agent.agent_description}
                          </Box>
                        </Box>
                      </Checkbox>
                    </div>
                  ))}
                </SpaceBetween>
              )}
            </div>
            {!isLoadingAgents && availableAgents.length > 0 && (
              <Box padding={{ top: 's' }}>
                <Button
                  formAction="none"
                  variant={selectedAgents.length === availableAgents.length ? 'normal' : 'primary'}
                  onClick={handleSelectAllAgents}
                >
                  {selectedAgents.length === availableAgents.length ? (
                    <FormattedMessage {...messages.deselectAll} />
                  ) : (
                    <FormattedMessage {...messages.selectAll} />
                  )}
                </Button>
              </Box>
            )}
            {hoveredAgent && (
              <div
                style={{
                  position: 'fixed',
                  top: mousePosition.y + 10,
                  left: mousePosition.x + 10,
                  backgroundColor: 'white',
                  border: '1px solid #d5dbdb',
                  borderRadius: '4px',
                  padding: '16px',
                  boxShadow: '0 4px 12px rgba(0,0,0,0.2)',
                  zIndex: 9999,
                  minWidth: '300px',
                  maxWidth: '500px',
                }}
              >
                {(() => {
                  const agent = availableAgents.find((a) => a.agent_id === hoveredAgent);
                  if (!agent) return null;
                  return (
                    <>
                      <Box fontWeight="bold" fontSize="body-m" padding={{ bottom: 's' }}>
                        {agent.agent_name}
                      </Box>
                      <Box fontSize="body-s" color="text-body-secondary" padding={{ bottom: 's' }}>
                        {agent.agent_description}
                      </Box>
                      {agent.sample_queries && agent.sample_queries.length > 0 && (
                        <Box>
                          <Box fontWeight="bold" fontSize="body-s" padding={{ bottom: 'xs' }}>
                            <FormattedMessage {...messages.sampleQueries} />
                          </Box>
                          <SpaceBetween size="xs">
                            {agent.sample_queries.map((query) => (
                              <Box key={query} fontSize="body-s" color="text-body-secondary">
                                • {query}
                              </Box>
                            ))}
                          </SpaceBetween>
                        </Box>
                      )}
                    </>
                  );
                })()}
              </div>
            )}
          </Box>

          <Grid gridDefinition={[{ colspan: { default: 12, xxs: 9 } }, { colspan: { default: 12, xxs: 3 } }]}>
            <FormField label={intl.formatMessage(messages.questionLabel)}>
              <Textarea
                placeholder={
                  selectedAgents.length > 0
                    ? availableAgents.find((agent) => agent.agent_id === selectedAgents[0])?.sample_queries?.[0] ||
                      intl.formatMessage(messages.placeholder)
                    : intl.formatMessage(messages.placeholderNoAgent)
                }
                value={currentInputText}
                onChange={({ detail }) => updateAnalyticsState({ currentInputText: detail.value })}
                disabled={isSubmitting}
                rows={3}
                className="expandable-textarea"
              />
            </FormField>
            <Box padding={{ top: 'xl' }}>
              <SpaceBetween size="s">
                <Button
                  variant="primary"
                  {...({ type: 'submit' } as Record<string, unknown>)}
                  disabled={!currentInputText.trim() || selectedAgents.length === 0 || isSubmitting}
                  fullWidth
                >
                  {isSubmitting ? <FormattedMessage {...messages.submitting} /> : <FormattedMessage {...messages.submit} />}
                </Button>
                <Button variant="normal" onClick={handleClearQuery} disabled={isSubmitting} fullWidth>
                  <FormattedMessage {...messages.clear} />
                </Button>
              </SpaceBetween>
            </Box>
          </Grid>

          <FormField>
            <ButtonDropdown
              items={createDropdownItems() as unknown as ButtonDropdownProps.ItemOrGroup[]}
              onItemClick={handleDropdownItemClick}
              {...({ onFocus: () => fetchQueryHistory() } as Record<string, unknown>)}
              loading={isLoadingHistory}
              disabled={isSubmitting}
            >
              {(() => {
                if (!selectedOption) return <FormattedMessage {...messages.historyButton} />;
                if (selectedOption.label?.length > 40) {
                  return `${selectedOption.label.substring(0, 40)}...`;
                }
                return selectedOption.label || <FormattedMessage {...messages.historySelected} />;
              })()}
            </ButtonDropdown>
          </FormField>
        </SpaceBetween>
      </form>

      <Modal
        onDismiss={() => setShowMcpInfoModal(false)}
        visible={showMcpInfoModal}
        header={
          <Header>
            <FormattedMessage {...messages.mcpTitle} />
          </Header>
        }
        footer={
          <Box float="right">
            <Button variant="primary" onClick={() => setShowMcpInfoModal(false)}>
              <FormattedMessage {...messages.mcpClose} />
            </Button>
          </Box>
        }
      >
        <SpaceBetween size="m">
          <Box>
            <Box fontWeight="bold" fontSize="body-m">
              <FormattedMessage {...messages.mcpWhatTitle} />
            </Box>
            <Box>
              <FormattedMessage {...messages.mcpWhatBody} />
            </Box>
          </Box>

          <Box>
            <Box fontWeight="bold" fontSize="body-m">
              <FormattedMessage {...messages.mcpAddingTitle} />
            </Box>
            <Box>
              <FormattedMessage {...messages.mcpAddingBody} />
            </Box>
          </Box>

          <Box>
            <Box fontWeight="bold" fontSize="body-m">
              <FormattedMessage {...messages.mcpLearnMoreTitle} />
            </Box>
            <Box>
              <FormattedMessage {...messages.mcpLearnMoreBody} values={{ link: mcpDocsLink }} />
            </Box>
          </Box>
        </SpaceBetween>
      </Modal>
    </>
  );
};

export default AgentQueryInput;
