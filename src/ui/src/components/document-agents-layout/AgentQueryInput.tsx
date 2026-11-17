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
const textareaStyles = `
  .expandable-textarea {
    max-height: 250px;
    overflow-y: auto !important;
    resize: vertical;
  }
`;

const logger = new ConsoleLogger('AgentQueryInput');

const AgentQueryInput = ({ onSubmit, isSubmitting = false, selectedResult = null }: AgentQueryInputProps): React.JSX.Element => {
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
        return 'Unknown date';
      }
      return formatDateTime(date);
    } catch (e) {
      logger.warn(`Error formatting date: ${dateString}`, e);
      return 'Unknown date';
    }
  };

  // Create dropdown items with delete functionality
  const createDropdownItems = (): { id?: string; text: string | React.ReactNode; disabled: boolean }[] => {
    if (queryHistory.length === 0) {
      return [{ text: 'No previous questions found', disabled: true }];
    }

    return queryHistory.map((job) => {
      const displayText = job.query?.length > 50 ? `${job.query.substring(0, 50)}...` : job.query || 'No query';
      const dateText = formatDate(job.createdAt);

      return {
        id: job.jobId,
        text: (
          <div style={{ display: 'flex', alignItems: 'center', width: '100%', minHeight: '40px' }}>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontWeight: 'normal', marginBottom: '2px' }}>{displayText}</div>
              <div style={{ fontSize: '12px', color: '#5f6b7a' }}>
                {dateText} • {job.status === 'COMPLETED' ? 'Completed' : job.status || 'Unknown status'}
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
              ariaLabel={`Delete query: ${displayText}`}
            />
          </div>
        ),
        disabled: false,
      };
    });
  };

  return (
    <>
      <style>{textareaStyles}</style>
      <form onSubmit={handleSubmit}>
        <SpaceBetween size="l">
          <Box>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <Box fontSize="heading-xs" fontWeight="bold">
                Select from available agents
              </Box>
              <Button variant="normal" onClick={() => setShowMcpInfoModal(true)} {...({ fontSize: 'body-s' } as Record<string, unknown>)}>
                🚀 NEW: Integrate your own systems with MCP!
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
                  Loading agents...
                </Box>
              )}
              {!isLoadingAgents && availableAgents.length === 0 && (
                <Box textAlign="center" padding="m">
                  <b>No agents available</b>
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
                  {selectedAgents.length === availableAgents.length ? 'Deselect All Agents' : 'Select All Agents'}
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
                            Sample Queries:
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
            <FormField label="Enter your question for the agent">
              <Textarea
                placeholder={
                  selectedAgents.length > 0
                    ? availableAgents.find((agent) => agent.agent_id === selectedAgents[0])?.sample_queries?.[0] ||
                      'Enter your question here...'
                    : 'Select at least one available agent to get started'
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
                  {isSubmitting ? 'Submitting...' : 'Submit question'}
                </Button>
                <Button variant="normal" onClick={handleClearQuery} disabled={isSubmitting} fullWidth>
                  Clear question
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
                if (!selectedOption) return 'Select a previous question';
                if (selectedOption.label?.length > 40) {
                  return `${selectedOption.label.substring(0, 40)}...`;
                }
                return selectedOption.label || 'Selected question';
              })()}
            </ButtonDropdown>
          </FormField>
        </SpaceBetween>
      </form>

      <Modal
        onDismiss={() => setShowMcpInfoModal(false)}
        visible={showMcpInfoModal}
        header={<Header>Custom MCP Agents</Header>}
        footer={
          <Box float="right">
            <Button variant="primary" onClick={() => setShowMcpInfoModal(false)}>
              Close
            </Button>
          </Box>
        }
      >
        <SpaceBetween size="m">
          <Box>
            <Box fontWeight="bold" fontSize="body-m">
              What are MCP Agents?
            </Box>
            <Box>
              Model Context Protocol (MCP) agents allow you to connect external tools and services to extend the capabilities of your
              document analysis workflow.
            </Box>
          </Box>

          <Box>
            <Box fontWeight="bold" fontSize="body-m">
              Adding Custom Agents
            </Box>
            <Box>
              You can add your own MCP agents by configuring external MCP servers in AWS Secrets Manager. This allows you to integrate
              custom tools, APIs, and services specific to your organization&apos;s needs without any code changes or redeployments.
            </Box>
          </Box>

          <Box>
            <Box fontWeight="bold" fontSize="body-m">
              Learn More
            </Box>
            <Box>
              For detailed setup instructions and examples, see the{' '}
              <Link
                external
                href="https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/main/docs/custom-MCP-agent.md"
              >
                Custom MCP Agent Documentation
              </Link>
            </Box>
          </Box>
        </SpaceBetween>
      </Modal>
    </>
  );
};

export default AgentQueryInput;
