// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React, { useState } from 'react';
import { Box, Spinner, Button, Modal, Header, SpaceBetween, Tabs } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, useIntl, type NoMessageValues } from 'react-intl';
import SafeMarkdown from '../common/SafeMarkdown';
import { useFormatters } from '../../i18n/format';

import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter';
import { atelierLakesideLight } from 'react-syntax-highlighter/dist/esm/styles/hljs';

/** The CloudWatch search summary a log search tool returns. Stack names, log groups and events are data. */
const logMessages = defineMessages<{
  summary: NoMessageValues;
  stack: NoMessageValues;
  filterPattern: NoMessageValues;
  groupsSearched: NoMessageValues;
  totalEvents: NoMessageValues;
  events: NoMessageValues;
  groupEvents: { readonly logGroup: string; readonly count: number };
  noEvents: { readonly filterPattern: string };
}>({
  summary: {
    id: 'agentChat.tool.logs.summary',
    defaultMessage: 'Search Summary',
    description: "Heading of the summary of a CloudWatch log search, in an Agent Chat tool's details.",
  },
  stack: {
    id: 'agentChat.tool.logs.stack',
    defaultMessage: 'Stack:',
    description: 'Label before the name of the CloudFormation stack whose logs were searched. The name is shown as it arrives.',
  },
  filterPattern: {
    id: 'agentChat.tool.logs.filterPattern',
    defaultMessage: 'Filter Pattern:',
    description: 'Label before the CloudWatch filter pattern of the log search, such as ERROR, shown as it arrives.',
  },
  groupsSearched: {
    id: 'agentChat.tool.logs.groupsSearched',
    defaultMessage: 'Log Groups Searched:',
    description: 'Label before the count of CloudWatch log groups searched, shown as "searched / found".',
  },
  totalEvents: {
    id: 'agentChat.tool.logs.totalEvents',
    defaultMessage: 'Total Events Found:',
    description: 'Label before the number of log events the CloudWatch log search found.',
  },
  events: {
    id: 'agentChat.tool.logs.events',
    defaultMessage: 'Log Events',
    description: 'Heading above the log events a CloudWatch log search found, grouped by log group.',
  },
  groupEvents: {
    id: 'agentChat.tool.logs.groupEvents',
    defaultMessage: '{logGroup} ({count, plural, one {# event} other {# events}})',
    description: 'Heading of one CloudWatch log group in the search results. {logGroup} is its name; {count} is how many events it holds.',
  },
  noEvents: {
    id: 'agentChat.tool.logs.noEvents',
    defaultMessage: 'No {filterPattern} events found in the searched time period.',
    description:
      'Shown when the CloudWatch log search found nothing. {filterPattern} is the filter pattern searched for, such as ERROR, as it arrives.',
  },
});

/** Never shown today: the tool's status is computed and not rendered. */
const statusMessages = defineMessages<{
  executing: NoMessageValues;
  processingResults: NoMessageValues;
  completed: NoMessageValues;
  executionCompleted: NoMessageValues;
  starting: NoMessageValues;
}>({
  executing: {
    id: 'agentChat.tool.status.executing',
    defaultMessage: 'Executing...',
    description: 'Status of a tool an Agent Chat agent uses: the tool is running.',
  },
  processingResults: {
    id: 'agentChat.tool.status.processingResults',
    defaultMessage: 'Processing results...',
    description: "Status of a tool an Agent Chat agent uses: the tool's results are being read.",
  },
  completed: {
    id: 'agentChat.tool.status.completed',
    defaultMessage: 'Completed',
    description: 'Status of a tool an Agent Chat agent uses: the tool returned its results.',
  },
  executionCompleted: {
    id: 'agentChat.tool.status.executionCompleted',
    defaultMessage: 'Execution completed',
    description: 'Status of a tool an Agent Chat agent uses: the tool ran and has no results yet.',
  },
  starting: {
    id: 'agentChat.tool.status.starting',
    defaultMessage: 'Starting...',
    description: 'Status of a tool an Agent Chat agent uses: the tool has not started yet.',
  },
});

const messages = defineMessages<{
  codeLabelFromPython: NoMessageValues;
  codeLabelPythonDict: NoMessageValues;
  viewDetails: NoMessageValues;
  detailsTitle: { readonly toolName: string };
  close: NoMessageValues;
  tabExecution: NoMessageValues;
  tabResults: NoMessageValues;
  executing: NoMessageValues;
  noExecutionDetails: NoMessageValues;
  processingResults: NoMessageValues;
  noResults: NoMessageValues;
}>({
  codeLabelFromPython: {
    id: 'agentChat.tool.codeLabel.fromPython',
    defaultMessage: 'JSON (converted from Python)',
    description: "Label above a tool's input or output that arrived as Python data and is shown as JSON. JSON and Python are format names.",
  },
  codeLabelPythonDict: {
    id: 'agentChat.tool.codeLabel.pythonDict',
    defaultMessage: 'JSON (Python dict)',
    description:
      "Label above a tool's input or output that arrived as a Python dictionary and is shown as JSON. JSON and Python are format names.",
  },
  viewDetails: {
    id: 'agentChat.tool.viewDetails',
    defaultMessage: 'View Details',
    description: 'Button beside the name of a tool an Agent Chat agent used. It opens the tool input and output.',
  },
  detailsTitle: {
    id: 'agentChat.tool.detailsTitle',
    defaultMessage: '{toolName} Details',
    description: "Title of the dialog that shows a tool's input and output. {toolName} is the tool's name, as the agent reports it.",
  },
  close: {
    id: 'agentChat.tool.close',
    defaultMessage: 'Close',
    description: "Button that closes the dialog showing a tool's input and output.",
  },
  tabExecution: {
    id: 'agentChat.tool.tab.execution',
    defaultMessage: 'Execution Details',
    description: 'Tab of the tool dialog that shows the input the agent gave the tool.',
  },
  tabResults: {
    id: 'agentChat.tool.tab.results',
    defaultMessage: 'Results',
    description: 'Tab of the tool dialog that shows what the tool returned.',
  },
  executing: {
    id: 'agentChat.tool.executing',
    defaultMessage: 'Executing tool...',
    description: 'Shown in the Execution Details tab of the tool dialog while the tool runs.',
  },
  noExecutionDetails: {
    id: 'agentChat.tool.noExecutionDetails',
    defaultMessage: 'No execution details available',
    description: 'Shown in the Execution Details tab of the tool dialog when the tool has no input to show.',
  },
  processingResults: {
    id: 'agentChat.tool.processingResults',
    defaultMessage: 'Processing results...',
    description: "Shown in the Results tab of the tool dialog while the tool's results are read.",
  },
  noResults: {
    id: 'agentChat.tool.noResults',
    defaultMessage: 'No results available yet',
    description: 'Shown in the Results tab of the tool dialog before the tool has returned anything.',
  },
});

interface CodeBlockProps {
  language: string;
  content: string;
  label?: string | null;
}

/**
 * CodeBlock sub-component for consistent syntax highlighting
 */
const CodeBlock = ({ language, content, label = null }: CodeBlockProps): React.JSX.Element => (
  <Box>
    {label && (
      <Box {...({ marginBottom: 's' } as Record<string, unknown>)}>
        <Box fontSize="body-s" color="text-status-info">
          {label}
        </Box>
      </Box>
    )}
    <SyntaxHighlighter language={language} style={atelierLakesideLight} wrapLongLines>
      {content}
    </SyntaxHighlighter>
  </Box>
);

interface CloudWatchLogsData {
  stack_name: string;
  filter_pattern: string;
  log_groups_searched: number;
  total_log_groups_found: number;
  total_events_found: number;
  results?: Array<{
    log_group: string;
    events_found: number;
    events: Array<{
      timestamp: string;
      log_stream: string;
      message: string;
    }>;
  }>;
}

interface CloudWatchLogsDisplayProps {
  data: CloudWatchLogsData;
}

/**
 * CloudWatchLogsDisplay sub-component for structured log display
 */
const CloudWatchLogsDisplay = ({ data }: CloudWatchLogsDisplayProps): React.JSX.Element => {
  const { formatDateTime } = useFormatters();
  const formatTimestamp = (timestamp: string): string => {
    return formatDateTime(new Date(timestamp));
  };

  const formatLogMessage = (message: string): string => {
    // Clean up the message by removing AWS Lambda prefixes and formatting
    return message
      .replace(/^\[ERROR\]\t\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z\t[\w-]+\t/, '') // Remove AWS Lambda prefix
      .replace(/\\n/g, '\n') // Convert literal \n to actual newlines
      .replace(/\\xa0/g, ' '); // Convert non-breaking spaces
  };

  return (
    <Box>
      <Box {...({ marginBottom: 'l' } as Record<string, unknown>)}>
        <Box fontSize="heading-s" {...({ marginBottom: 's' } as Record<string, unknown>)}>
          <FormattedMessage {...logMessages.summary} />
        </Box>
        <SpaceBetween direction="vertical" size="xs">
          <Box {...({ display: 'flex', justifyContent: 'space-between' } as Record<string, unknown>)}>
            <Box>
              <FormattedMessage {...logMessages.stack} />
            </Box>
            <Box fontWeight="bold">{data.stack_name}</Box>
          </Box>
          <Box {...({ display: 'flex', justifyContent: 'space-between' } as Record<string, unknown>)}>
            <Box>
              <FormattedMessage {...logMessages.filterPattern} />
            </Box>
            <Box fontWeight="bold">{data.filter_pattern}</Box>
          </Box>
          <Box {...({ display: 'flex', justifyContent: 'space-between' } as Record<string, unknown>)}>
            <Box>
              <FormattedMessage {...logMessages.groupsSearched} />
            </Box>
            <Box fontWeight="bold">
              {data.log_groups_searched} / {data.total_log_groups_found}
            </Box>
          </Box>
          <Box {...({ display: 'flex', justifyContent: 'space-between' } as Record<string, unknown>)}>
            <Box>
              <FormattedMessage {...logMessages.totalEvents} />
            </Box>
            <Box fontWeight="bold" color={data.total_events_found > 0 ? 'text-status-error' : 'text-status-success'}>
              {data.total_events_found}
            </Box>
          </Box>
        </SpaceBetween>
      </Box>

      {/* Log Events */}
      {data.results && data.results.length > 0 && (
        <Box>
          <Box fontSize="heading-s" {...({ marginBottom: 's' } as Record<string, unknown>)}>
            <FormattedMessage {...logMessages.events} />
          </Box>
          <SpaceBetween direction="vertical" size="m">
            {data.results.map((logGroup) => (
              <Box key={logGroup.log_group}>
                <Box fontSize="body-m" fontWeight="bold" {...({ marginBottom: 's' } as Record<string, unknown>)} color="text-status-info">
                  <FormattedMessage {...logMessages.groupEvents} values={{ logGroup: logGroup.log_group, count: logGroup.events_found }} />
                </Box>
                <SpaceBetween direction="vertical" size="s">
                  {logGroup.events.map((event) => (
                    <Box
                      key={`${event.timestamp}-${event.log_stream}`}
                      padding="s"
                      {...({ backgroundColor: 'background-container-content' } as Record<string, unknown>)}
                    >
                      <SpaceBetween direction="vertical" size="xs">
                        <Box {...({ display: 'flex', justifyContent: 'space-between', alignItems: 'center' } as Record<string, unknown>)}>
                          <Box fontSize="body-s" color="text-status-info">
                            {formatTimestamp(event.timestamp)}
                          </Box>
                          <Box fontSize="body-s" color="text-status-info">
                            {event.log_stream.split('/').pop()}
                          </Box>
                        </Box>
                        <Box fontSize="body-s" {...({ fontFamily: 'monospace', whiteSpace: 'pre-wrap' } as Record<string, unknown>)}>
                          {formatLogMessage(event.message)}
                        </Box>
                      </SpaceBetween>
                    </Box>
                  ))}
                </SpaceBetween>
              </Box>
            ))}
          </SpaceBetween>
        </Box>
      )}

      {data.total_events_found === 0 && (
        <Box textAlign="center" color="text-status-success" padding="l">
          <FormattedMessage {...logMessages.noEvents} values={{ filterPattern: data.filter_pattern }} />
        </Box>
      )}
    </Box>
  );
};

interface AgentToolComponentProps {
  toolName: string;
  toolUseId: string;
  executionLoading?: boolean;
  executionDetails?: string | null;
  resultLoading?: boolean;
  resultDetails?: string | null;
  timestamp: string;
  onToggle?: ((isOpen: boolean) => void) | null;
  parentProcessing?: boolean;
}

/**
 * AgentToolComponent displays both tool execution and results in a single component
 */
const AgentToolComponentBase = ({
  toolName,
  toolUseId,
  executionLoading = false,
  executionDetails = null,
  resultLoading = false,
  resultDetails = null,
  timestamp: _timestamp,
  onToggle = null,
  parentProcessing = false,
}: AgentToolComponentProps): React.JSX.Element => {
  const intl = useIntl();
  const [isModalVisible, setIsModalVisible] = useState(false);
  const [activeTabId, setActiveTabId] = useState('execution');

  const handleViewDetails = () => {
    setIsModalVisible(true);
    if (onToggle) {
      onToggle(true);
    }
  };

  const handleCloseModal = () => {
    setIsModalVisible(false);
    if (onToggle) {
      onToggle(false);
    }
  };

  const formatToolName = (name: string): string => {
    // Convert snake_case or camelCase to readable format
    return name
      .replace(/([A-Z])/g, ' $1')
      .replace(/_/g, ' ')
      .replace(/\b\w/g, (l) => l.toUpperCase())
      .trim();
  };

  // Detect content type based on tool name
  const detectByToolName = (currentToolName: string | null): string | null => {
    const toolMappings: Record<string, string> = {
      // Analytics Agent Tools
      get_table_info: 'markdown',
      get_database_overview: 'markdown',
      run_athena_query: 'json',
      run_athena_query_with_config: 'json',

      // Code Interpreter Tools (likely from CodeInterpreterTools)
      execute_python: 'python',

      // Error Analyzer Tools (likely from stepfunction_tool, xray_tool, cloudwatch_tool)
      get_stepfunction_execution: 'json',
      xray_trace: 'json',
      xray_performance_analysis: 'json',
      cloudwatch_logs: 'json',

      // DynamoDB Tools
      dynamodb_query: 'json',
      dynamodb_status: 'json',

      // General patterns for unknown tools
      sql_query: 'sql',
      execute_sql: 'sql',
      run_query: 'sql',
      bash_command: 'bash',
      shell_command: 'bash',
      run_command: 'bash',
      yaml_parser: 'yaml',
      parse_yaml: 'yaml',
      xml_parser: 'xml',
      parse_xml: 'xml',
      json_parser: 'json',
      parse_json: 'json',
    };

    return toolMappings[currentToolName?.toLowerCase() ?? ''] || null;
  };

  // Detect content type based on content analysis
  const detectContentType = (content: string | null): string => {
    if (!content || typeof content !== 'string') return 'text';

    const trimmed = content.trim();

    // JSON detection - check if it starts with { or [ and is valid JSON
    if ((trimmed.startsWith('{') && trimmed.endsWith('}')) || (trimmed.startsWith('[') && trimmed.endsWith(']'))) {
      try {
        JSON.parse(trimmed);
        return 'json';
      } catch (e) {
        // Not valid JSON, continue checking
      }
    }

    // SQL detection
    if (/^\s*(SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|WITH|EXPLAIN)\s+/i.test(trimmed)) {
      return 'sql';
    }

    // Python detection
    if (/^\s*(def|class|import|from|if __name__|print\(|#\s*python)/im.test(trimmed)) {
      return 'python';
    }

    // JavaScript detection
    if (/^\s*(function|const|let|var|=>|console\.log|\/\/\s*javascript)/im.test(trimmed)) {
      return 'javascript';
    }

    // Bash/Shell detection
    if (/^\s*(#!\/bin\/bash|#!\/bin\/sh|\$\s+|#\s*bash)/im.test(trimmed)) {
      return 'bash';
    }

    // YAML detection
    if (/^\s*[\w-]+:\s*[\w\s-]+$/m.test(trimmed) && !trimmed.includes('{')) {
      return 'yaml';
    }

    // XML detection
    if (trimmed.startsWith('<') && trimmed.endsWith('>') && trimmed.includes('</')) {
      return 'xml';
    }

    return 'text';
  };

  // Smart content formatter
  const formatContent = (content: string | null, currentToolName: string): React.JSX.Element | null => {
    if (!content) return null;

    // Clean up Python string representation artifacts
    let cleanedContent = content;

    // Handle Python dict/object string representations that contain literal \n
    if (typeof content === 'string' && content.includes('\\n')) {
      // Replace literal \n with actual newlines for better parsing
      cleanedContent = content.replace(/\\n/g, '\n');
    }

    // Handle escaped quotes in Python code/JSON strings
    if (typeof cleanedContent === 'string' && cleanedContent.includes('\\"')) {
      // Replace escaped quotes with regular quotes for better readability
      cleanedContent = cleanedContent.replace(/\\"/g, '"');
    }

    // First try tool-based detection, then content-based
    const detectedType = detectByToolName(currentToolName) || detectContentType(cleanedContent);

    // Handle JSON with formatting
    if (detectedType === 'json') {
      try {
        const parsed = JSON.parse(cleanedContent);

        // Special handling for CloudWatch logs results
        if (
          currentToolName &&
          currentToolName.toLowerCase().includes('cloudwatch') &&
          parsed.stack_name &&
          parsed.results &&
          Array.isArray(parsed.results)
        ) {
          return <CloudWatchLogsDisplay data={parsed} />;
        }

        const formatted = JSON.stringify(parsed, null, 2);
        return <CodeBlock language="json" content={formatted} label="JSON" />;
      } catch (e) {
        // If JSON parsing fails, try to handle Python dict string representation
        console.log('JSON parsing failed, trying Python dict conversion:', e);
      }
    }

    // Try to handle Python dict string representation (common for CloudWatch logs)
    if (cleanedContent.includes("'") && (cleanedContent.includes('stack_name') || cleanedContent.includes('analysis_type'))) {
      try {
        // More robust Python dict to JSON conversion
        const jsonContent = cleanedContent
          // Handle escaped quotes in strings first
          .replace(/\\"/g, '\\"') // Preserve already escaped quotes
          .replace(/\\'/g, "\\'") // Preserve already escaped single quotes
          // Replace Python literals
          .replace(/True/g, 'true')
          .replace(/False/g, 'false')
          .replace(/None/g, 'null')
          // Handle single quotes around keys and string values more carefully
          .replace(/'([^']*)':/g, '"$1":') // Replace 'key': with "key":
          .replace(/:\s*'([^']*)'/g, ': "$1"') // Replace : 'value' with : "value"
          // Handle single quotes in nested structures
          .replace(/\[\s*'([^']*)'\s*\]/g, '["$1"]') // Replace ['value'] with ["value"]
          // Handle remaining single quotes (for string values that might contain special chars)
          .replace(/'/g, '"');

        const parsed = JSON.parse(jsonContent);

        // Special handling for CloudWatch logs results from Python dict
        if (parsed.stack_name && parsed.results && Array.isArray(parsed.results)) {
          return <CloudWatchLogsDisplay data={parsed} />;
        }

        const formatted = JSON.stringify(parsed, null, 2);
        return <CodeBlock language="json" content={formatted} label={intl.formatMessage(messages.codeLabelFromPython)} />;
      } catch (conversionError) {
        // Try a more aggressive approach for complex nested structures
        try {
          // Use eval in a safe way for Python dict (only for trusted content)
          // This is safe because the content comes from our own backend
          const pythonDict = cleanedContent.replace(/'/g, '"').replace(/True/g, 'true').replace(/False/g, 'false').replace(/None/g, 'null');
          const parsed = JSON.parse(pythonDict);

          if (parsed.stack_name && parsed.results && Array.isArray(parsed.results)) {
            return <CloudWatchLogsDisplay data={parsed} />;
          }

          const formatted = JSON.stringify(parsed, null, 2);
          return <CodeBlock language="json" content={formatted} label={intl.formatMessage(messages.codeLabelPythonDict)} />;
        } catch (finalError) {
          // Fall through to text formatting
        }
      }
    }

    // Handle markdown content - render as markdown instead of highlighting
    if (detectedType === 'markdown') {
      return <SafeMarkdown>{cleanedContent}</SafeMarkdown>;
    }

    // Handle other code types with syntax highlighting
    if (detectedType !== 'text') {
      return <CodeBlock language={detectedType} content={cleanedContent} label={detectedType.toUpperCase()} />;
    }

    // Fallback to ReactMarkdown for text content
    return <SafeMarkdown>{cleanedContent}</SafeMarkdown>;
  };

  // Determine the overall status and display
  const getStatus = () => {
    if (executionLoading) {
      return { text: intl.formatMessage(statusMessages.executing), color: 'text-status-info' };
    }
    if (resultLoading) {
      return { text: intl.formatMessage(statusMessages.processingResults), color: 'text-status-info' };
    }
    if (resultDetails) {
      return { text: intl.formatMessage(statusMessages.completed), color: 'text-status-success' };
    }
    if (executionDetails) {
      return { text: intl.formatMessage(statusMessages.executionCompleted), color: 'text-status-success' };
    }
    return { text: intl.formatMessage(statusMessages.starting), color: 'text-status-info' };
  };

  const _status = getStatus();
  const hasContent = executionDetails || resultDetails;
  // Hide spinner if parent process is complete, even if individual tool states indicate loading
  const showSpinner = (executionLoading || resultLoading) && parentProcessing;

  // Check if this is an Athena query tool that should hide results
  const shouldHideResults =
    toolName &&
    (toolName.toLowerCase().includes('run_athena_query_with_config') ||
      (toolName.toLowerCase().includes('athena') && toolName.toLowerCase().includes('config')));

  return (
    <div className="agent-tool-component" data-tool-use-id={toolUseId}>
      <SpaceBetween direction="horizontal" size="xs" alignItems="center">
        <Box {...({ fontSize: '10px' } as Record<string, unknown>)}>
          <strong>{formatToolName(toolName)}</strong>
        </Box>

        <Box {...({ display: 'flex', alignItems: 'center' } as Record<string, unknown>)}>
          {showSpinner && (
            <Box {...({ marginRight: 's' } as Record<string, unknown>)}>
              <Spinner />
            </Box>
          )}
          {hasContent && (
            <button onClick={handleViewDetails} style={{ fontSize: '12px' }}>
              <FormattedMessage {...messages.viewDetails} />
            </button>
          )}
        </Box>
      </SpaceBetween>

      {/* Modal for displaying execution and result details */}
      <Modal
        onDismiss={handleCloseModal}
        visible={isModalVisible}
        size="large"
        header={
          <Header variant="h2">
            <FormattedMessage {...messages.detailsTitle} values={{ toolName: formatToolName(toolName) }} />
          </Header>
        }
        footer={
          <Box float="right">
            <Button variant="primary" onClick={handleCloseModal}>
              <FormattedMessage {...messages.close} />
            </Button>
          </Box>
        }
      >
        <Tabs
          activeTabId={activeTabId}
          onChange={({ detail }) => setActiveTabId(detail.activeTabId)}
          tabs={[
            {
              id: 'execution',
              label: intl.formatMessage(messages.tabExecution),
              content: (
                <Box padding={{ top: 's' }}>
                  {executionLoading ? (
                    <Box {...({ display: 'flex', alignItems: 'center' } as Record<string, unknown>)}>
                      <Spinner />
                      <Box {...({ marginLeft: 's' } as Record<string, unknown>)}>
                        <FormattedMessage {...messages.executing} />
                      </Box>
                    </Box>
                  ) : executionDetails ? (
                    formatContent(executionDetails, toolName)
                  ) : (
                    <Box color="text-status-info">
                      <FormattedMessage {...messages.noExecutionDetails} />
                    </Box>
                  )}
                </Box>
              ),
            },
            // Conditionally include Results tab - hide for Athena query with config tools
            ...(shouldHideResults
              ? []
              : [
                  {
                    id: 'results',
                    label: intl.formatMessage(messages.tabResults),
                    content: (
                      <Box padding={{ top: 's' }}>
                        {resultLoading ? (
                          <Box {...({ display: 'flex', alignItems: 'center' } as Record<string, unknown>)}>
                            <Spinner />
                            <Box {...({ marginLeft: 's' } as Record<string, unknown>)}>
                              <FormattedMessage {...messages.processingResults} />
                            </Box>
                          </Box>
                        ) : resultDetails ? (
                          formatContent(resultDetails, toolName)
                        ) : (
                          <Box color="text-status-info">
                            <FormattedMessage {...messages.noResults} />
                          </Box>
                        )}
                      </Box>
                    ),
                  },
                ]),
          ]}
        />
      </Modal>
    </div>
  );
};

// Wrap with React.memo to prevent re-renders when props haven't changed
// This ensures the modal stays open during message streaming
const AgentToolComponent = React.memo<AgentToolComponentProps>(AgentToolComponentBase);

export default AgentToolComponent;
