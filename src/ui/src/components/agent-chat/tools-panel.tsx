// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React, { useState, useEffect } from 'react';
import { generateClient } from '../../api/client-shim';
import { ConsoleLogger } from 'aws-amplify/utils';
import {
  HelpPanel,
  SpaceBetween,
  Box,
  Button,
  StatusIndicator,
  Icon,
  ExpandableSection,
  Link,
  Modal,
  Header,
} from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, useIntl, type MessageTag, type NoMessageValues } from 'react-intl';
import { listAvailableAgents } from '../../graphql/generated';

const client = generateClient();
const logger = new ConsoleLogger('AgentChatToolsPanel');

interface Agent {
  agent_id: string;
  agent_name?: string;
  agent_description?: string;
  sample_queries?: string[];
}

const messages = defineMessages<{
  loadFailed: NoMessageValues;
  title: NoMessageValues;
  intro: NoMessageValues;
  loading: NoMessageValues;
  empty: NoMessageValues;
  description: NoMessageValues;
  sampleQueries: NoMessageValues;
  insertSampleQuery: { readonly query: string };
  mcpButton: NoMessageValues;
  learnMore: NoMessageValues;
  docsAgentChat: NoMessageValues;
  docsCustomMcp: NoMessageValues;
  docsMcpServer: NoMessageValues;
  mcpTitle: NoMessageValues;
  mcpClose: NoMessageValues;
  mcpWhatTitle: NoMessageValues;
  mcpWhatBody: NoMessageValues;
  mcpAddingTitle: NoMessageValues;
  mcpAddingBody: NoMessageValues;
  mcpLearnMoreTitle: NoMessageValues;
  mcpLearnMoreBody: { readonly link: MessageTag };
}>({
  loadFailed: {
    id: 'helpPanel.agentChat.loadFailed',
    defaultMessage: 'Failed to load available agents',
    description: 'Error in the Agent Chat help panel when the list of agents could not be loaded.',
  },
  title: {
    id: 'helpPanel.agentChat.title',
    defaultMessage: 'Available Agents',
    description: 'Heading of the Agent Chat help panel, which lists the agents the chat can use.',
  },
  intro: {
    id: 'helpPanel.agentChat.intro',
    defaultMessage:
      'Ask questions about IDP and get answers about the code, features, and more. See available agents below for capabilities.',
    description: 'First paragraph of the Agent Chat help panel, above the list of agents. IDP is the name of the solution.',
  },
  loading: {
    id: 'helpPanel.agentChat.loading',
    defaultMessage: 'Loading agents...',
    description: 'Shown in the Agent Chat help panel while the list of agents loads.',
  },
  empty: {
    id: 'helpPanel.agentChat.empty',
    defaultMessage: 'No agents available',
    description: 'Shown in the Agent Chat help panel when the deployment has no agents.',
  },
  description: {
    id: 'helpPanel.agentChat.description',
    defaultMessage: 'Description:',
    description: "Label above an agent's description in the Agent Chat help panel. The description shows as the agent registry sends it.",
  },
  sampleQueries: {
    id: 'helpPanel.agentChat.sampleQueries',
    defaultMessage: 'Sample Queries:',
    description:
      "Label above an agent's sample questions in the Agent Chat help panel. Each question is a button that puts it into the chat input.",
  },
  insertSampleQuery: {
    id: 'helpPanel.agentChat.insertSampleQuery',
    defaultMessage: 'Insert sample query: {query}',
    description:
      'Screen-reader label of a sample-question button in the Agent Chat help panel, which puts the question into the chat input. {query} is the question, as the agent registry sends it.',
  },
  mcpButton: {
    id: 'helpPanel.agentChat.mcpButton',
    defaultMessage: 'NEW: Integrate your own systems with MCP!',
    description: 'Button in the Agent Chat help panel that explains custom MCP agents. A rocket emoji is shown before it.',
  },
  learnMore: {
    id: 'helpPanel.agentChat.learnMore',
    defaultMessage: 'Learn more',
    description: 'Heading of the documentation links at the bottom of the Agent Chat help panel.',
  },
  docsAgentChat: {
    id: 'helpPanel.agentChat.docsAgentChat',
    defaultMessage: 'Agent Companion Chat',
    description: 'Documentation link in the Agent Chat help panel: the page about the Agent Companion Chat.',
  },
  docsCustomMcp: {
    id: 'helpPanel.agentChat.docsCustomMcp',
    defaultMessage: 'Custom MCP Agents',
    description: 'Documentation link in the Agent Chat help panel: the page about adding your own MCP agents.',
  },
  docsMcpServer: {
    id: 'helpPanel.agentChat.docsMcpServer',
    defaultMessage: 'MCP Server',
    description: "Documentation link in the Agent Chat help panel: the page about the solution's MCP server.",
  },
  mcpTitle: {
    id: 'helpPanel.agentChat.mcp.title',
    defaultMessage: 'Custom MCP Agents',
    description: 'Title of the dialog, opened from the Agent Chat help panel, that explains custom MCP agents.',
  },
  mcpClose: {
    id: 'helpPanel.agentChat.mcp.close',
    defaultMessage: 'Close',
    description: 'Button that closes the custom MCP agents dialog of the Agent Chat help panel.',
  },
  mcpWhatTitle: {
    id: 'helpPanel.agentChat.mcp.whatTitle',
    defaultMessage: 'What are MCP Agents?',
    description: 'Heading in the custom MCP agents dialog of the Agent Chat help panel.',
  },
  mcpWhatBody: {
    id: 'helpPanel.agentChat.mcp.whatBody',
    defaultMessage:
      'Model Context Protocol (MCP) agents allow you to connect external tools and services to extend the capabilities of your document analysis workflow.',
    description: 'Paragraph in the custom MCP agents dialog of the Agent Chat help panel, under "What are MCP Agents?".',
  },
  mcpAddingTitle: {
    id: 'helpPanel.agentChat.mcp.addingTitle',
    defaultMessage: 'Adding Custom Agents',
    description: 'Heading in the custom MCP agents dialog of the Agent Chat help panel.',
  },
  mcpAddingBody: {
    id: 'helpPanel.agentChat.mcp.addingBody',
    defaultMessage:
      "You can add your own MCP agents by configuring external MCP servers in AWS Secrets Manager. This allows you to integrate custom tools, APIs, and services specific to your organization's needs without any code changes or redeployments.",
    description:
      'Paragraph in the custom MCP agents dialog of the Agent Chat help panel, under "Adding Custom Agents". AWS Secrets Manager is a product name.',
  },
  mcpLearnMoreTitle: {
    id: 'helpPanel.agentChat.mcp.learnMoreTitle',
    defaultMessage: 'Learn More',
    description: 'Heading in the custom MCP agents dialog of the Agent Chat help panel, above the link to the documentation.',
  },
  mcpLearnMoreBody: {
    id: 'helpPanel.agentChat.mcp.learnMoreBody',
    defaultMessage: 'For detailed setup instructions and examples, see the <link>Custom MCP Agent Documentation</link>',
    description:
      'Last paragraph of the custom MCP agents dialog of the Agent Chat help panel. The text inside <link> links to the custom MCP agent documentation.',
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

const ToolsPanel = (): React.JSX.Element => {
  const intl = useIntl();
  const [agents, setAgents] = useState<Agent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expandedAgents, setExpandedAgents] = useState<Record<string, boolean>>({});
  const [showMcpInfoModal, setShowMcpInfoModal] = useState(false);

  useEffect(() => {
    const fetchAgents = async () => {
      try {
        setLoading(true);
        const response = await client.graphql({
          query: listAvailableAgents,
        });

        const agentsList = ((response as { data: Record<string, unknown> })?.data?.listAvailableAgents as Agent[]) || [];
        logger.debug('Fetched agents:', agentsList);
        setAgents(agentsList);
        setError(null);
      } catch (err) {
        logger.error('Error fetching agents:', err);
        setError(intl.formatMessage(messages.loadFailed));
      } finally {
        setLoading(false);
      }
    };

    fetchAgents();
  }, [intl]);

  const handleSampleQueryClick = (query: string): void => {
    // Dispatch a custom event that the chat input can listen to
    window.dispatchEvent(new CustomEvent('insertSampleQuery', { detail: { query } }));
  };

  const handleAgentToggle = (agentId: string, expanded: boolean): void => {
    setExpandedAgents((prev) => ({
      ...prev,
      [agentId]: expanded,
    }));
  };

  return (
    <HelpPanel
      header={
        <h2>
          <FormattedMessage {...messages.title} />
        </h2>
      }
    >
      <SpaceBetween size="l">
        <Box>
          <p>
            <FormattedMessage {...messages.intro} />
          </p>
        </Box>

        <Box>
          {loading && (
            <Box textAlign="center">
              <StatusIndicator type="loading">
                <FormattedMessage {...messages.loading} />
              </StatusIndicator>
            </Box>
          )}

          {error && (
            <Box textAlign="center">
              <StatusIndicator type="error">{error}</StatusIndicator>
            </Box>
          )}

          {!loading && !error && agents.length === 0 && (
            <Box textAlign="center">
              <StatusIndicator type="info">
                <FormattedMessage {...messages.empty} />
              </StatusIndicator>
            </Box>
          )}

          {!loading && !error && agents.length > 0 && (
            <SpaceBetween size="s">
              {agents.map((agent) => (
                <ExpandableSection
                  key={agent.agent_id}
                  headerText={agent.agent_name || agent.agent_id}
                  expanded={expandedAgents[agent.agent_id] || false}
                  onChange={({ detail }) => handleAgentToggle(agent.agent_id, detail.expanded)}
                  variant="default"
                >
                  <SpaceBetween size="s">
                    {agent.agent_description && (
                      <Box>
                        <strong>
                          <FormattedMessage {...messages.description} />
                        </strong>
                        <br />
                        {agent.agent_description}
                      </Box>
                    )}

                    {agent.sample_queries && agent.sample_queries.length > 0 && (
                      <Box>
                        <strong>
                          <FormattedMessage {...messages.sampleQueries} />
                        </strong>
                        <SpaceBetween size="xs">
                          {agent.sample_queries.map((query) => (
                            <Button
                              key={query}
                              variant="link"
                              iconName="copy"
                              onClick={() => handleSampleQueryClick(query)}
                              ariaLabel={intl.formatMessage(messages.insertSampleQuery, { query })}
                            >
                              {query}
                            </Button>
                          ))}
                        </SpaceBetween>
                      </Box>
                    )}
                  </SpaceBetween>
                </ExpandableSection>
              ))}
            </SpaceBetween>
          )}
        </Box>

        <Button variant="inline-link" onClick={() => setShowMcpInfoModal(true)} {...({ fontSize: 'body-s' } as Record<string, unknown>)}>
          🚀 <FormattedMessage {...messages.mcpButton} />
        </Button>

        <Box>
          <h3>
            <FormattedMessage {...messages.learnMore} /> <Icon name="external" />
          </h3>
          <ul>
            <li>
              <a
                href="https://aws-solutions-library-samples.github.io/accelerated-intelligent-document-processing-on-aws/agent-companion-chat/"
                target="_blank"
                rel="noopener noreferrer"
              >
                <FormattedMessage {...messages.docsAgentChat} />
              </a>
            </li>
            <li>
              <a
                href="https://aws-solutions-library-samples.github.io/accelerated-intelligent-document-processing-on-aws/custom-MCP-agent/"
                target="_blank"
                rel="noopener noreferrer"
              >
                <FormattedMessage {...messages.docsCustomMcp} />
              </a>
            </li>
            <li>
              <a
                href="https://aws-solutions-library-samples.github.io/accelerated-intelligent-document-processing-on-aws/mcp-server/"
                target="_blank"
                rel="noopener noreferrer"
              >
                <FormattedMessage {...messages.docsMcpServer} />
              </a>
            </li>
          </ul>
        </Box>
      </SpaceBetween>
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
    </HelpPanel>
  );
};

export default ToolsPanel;
