// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import React from 'react';
import { Box, Container, Header, SpaceBetween, Alert } from '@cloudscape-design/components';
import { ConsoleLogger } from 'aws-amplify/utils';
import { defineMessages, FormattedMessage, useIntl, type MessageTag, type NoMessageValues } from 'react-intl';

import PlotDisplay from './PlotDisplay';
import TableDisplay from './TableDisplay';
import TextDisplay from './TextDisplay';

const logger = new ConsoleLogger('AgentResultDisplay');

const messages = defineMessages<{
  noPlot: NoMessageValues;
  noTable: NoMessageValues;
  noText: NoMessageValues;
  rawResponse: NoMessageValues;
  unknownType: { readonly type: string };
  debugInfo: NoMessageValues;
  resultsFor: { readonly query: string; readonly i: MessageTag };
  responseType: { readonly type: string; readonly strong: MessageTag };
  unknownTypeName: NoMessageValues;
}>({
  noPlot: {
    id: 'agents.result.noPlot',
    defaultMessage: 'No plot data available',
    description: "Shown in place of an agent's chart result when the result holds no chart.",
  },
  noTable: {
    id: 'agents.result.noTable',
    defaultMessage: 'No table data available',
    description: "Shown in place of an agent's table result when the result holds no table.",
  },
  noText: {
    id: 'agents.result.noText',
    defaultMessage: 'No text data available',
    description: "Shown in place of an agent's text result when the result holds no text.",
  },
  rawResponse: {
    id: 'agents.result.rawResponse',
    defaultMessage: 'Raw Response',
    description: 'Heading above an agent result of a type the UI does not know, shown as raw data.',
  },
  unknownType: {
    id: 'agents.result.unknownType',
    defaultMessage: 'Unknown response type: {type}',
    description:
      'Warning above an agent result of a type the UI does not know. {type} is the type the agent returned, as is, or "undefined" when it returned none.',
  },
  debugInfo: {
    id: 'agents.result.debugInfo',
    defaultMessage: 'Debug Information:',
    description: 'Heading above the raw data of an agent result of a type the UI does not know.',
  },
  resultsFor: {
    id: 'agents.result.resultsFor',
    defaultMessage: 'Results for: <i>{query}</i>',
    description: "Heading of an agent's results. {query} is the question the agent answered, in italics and as it was sent.",
  },
  responseType: {
    id: 'agents.result.responseType',
    defaultMessage: 'Response type: <strong>{type}</strong>',
    description:
      "Notice above an agent's results. {type} is the result type the agent returned (plotData, table or text), in bold and as is, or the word for an unknown type.",
  },
  unknownTypeName: {
    id: 'agents.result.unknownTypeName',
    defaultMessage: 'Unknown',
    description: "Shown as the result type in the notice above an agent's results when the agent returned no type.",
  },
});

/** Renders an `<i>...</i>` element value: the question. */
const italic = (chunks: React.ReactNode[]): React.JSX.Element => <i>{chunks}</i>;

/** Renders a `<strong>...</strong>` element value: the result type. */
const strong = (chunks: React.ReactNode[]): React.JSX.Element => <strong>{chunks}</strong>;

interface AgentResultDisplayProps {
  result?: string | Record<string, unknown> | null;
  query?: string;
}

const AgentResultDisplay = ({ result = null, query = '' }: AgentResultDisplayProps): React.JSX.Element | null => {
  const intl = useIntl();

  if (!result) {
    return null;
  }

  logger.debug('Raw result received:', result);
  logger.debug('Result type:', typeof result);

  // Helper function to safely parse JSON strings with multiple levels
  const safeJsonParse = (data: unknown, fallback: unknown = null): unknown => {
    if (data === null || data === undefined) {
      return fallback;
    }

    // If it's already an object, return as is
    if (typeof data === 'object') {
      return data;
    }

    // If it's a string, try to parse it
    if (typeof data === 'string') {
      try {
        const parsed = JSON.parse(data);
        // If the parsed result is still a string, try parsing again (double-encoded JSON)
        if (typeof parsed === 'string') {
          try {
            return JSON.parse(parsed);
          } catch (innerError) {
            logger.debug('Inner JSON parse failed, returning first parse result:', innerError);
            return parsed;
          }
        }
        return parsed;
      } catch (error) {
        logger.warn('Failed to parse JSON string:', error);
        logger.debug('Failed string was:', data);
        return fallback;
      }
    }

    return data;
  };

  // Parse the result with enhanced logic
  let parsedResult = safeJsonParse(result, result) as Record<string, unknown>;

  logger.debug('Parsed result:', parsedResult);

  // Handle case where result might be wrapped in a "result" property (from DynamoDB)
  if (parsedResult && typeof parsedResult === 'object' && parsedResult.result) {
    logger.debug('Found nested result property, extracting...');
    parsedResult = safeJsonParse(parsedResult.result, parsedResult.result) as Record<string, unknown>;
    logger.debug('Extracted nested result:', parsedResult);
  }

  logger.debug('Parsed result:', parsedResult);

  const renderResultContent = (): React.JSX.Element => {
    const { responseType } = parsedResult;

    logger.debug('Rendering result with responseType:', responseType);

    switch (responseType) {
      case 'plotData': {
        logger.debug('Rendering plot data:', parsedResult);
        // Extract the first plot data item from the array
        const plotDataArray = parsedResult.plotData as Array<Record<string, unknown>> | undefined;
        const plotData = plotDataArray && plotDataArray.length > 0 ? plotDataArray[0] : null;
        return plotData ? (
          <PlotDisplay plotData={plotData} />
        ) : (
          <div>
            <FormattedMessage {...messages.noPlot} />
          </div>
        );
      }

      case 'table': {
        logger.debug('Rendering table data:', parsedResult);
        // Extract the nested tableData
        const tableData = (parsedResult.tableData || parsedResult) as Record<string, unknown>;
        return tableData ? (
          <TableDisplay tableData={tableData} />
        ) : (
          <div>
            <FormattedMessage {...messages.noTable} />
          </div>
        );
      }

      case 'text': {
        logger.debug('Rendering text data:', parsedResult);
        // Extract the nested textData or use the whole object if it has content
        const textData = (parsedResult.textData || parsedResult) as Record<string, unknown>;
        return textData ? (
          <TextDisplay textData={textData} />
        ) : (
          <div>
            <FormattedMessage {...messages.noText} />
          </div>
        );
      }

      default:
        // Fallback for legacy format or unknown response types
        logger.warn('Unknown or missing responseType:', responseType);
        logger.debug('Full parsed result for fallback:', parsedResult);
        return (
          <Box padding="m">
            <Header variant="h3">
              <FormattedMessage {...messages.rawResponse} />
            </Header>
            <Alert type="warning">
              {/* The agent's type as text, or the word "undefined" when it sent none, as before. */}
              <FormattedMessage {...messages.unknownType} values={{ type: String((responseType as string) || 'undefined') }} />
            </Alert>
            <Box padding="s">
              <Header {...({ variant: 'h4' } as Record<string, unknown>)}>
                <FormattedMessage {...messages.debugInfo} />
              </Header>
              <pre style={{ fontSize: '12px', maxHeight: '300px', overflow: 'auto' }}>{JSON.stringify(parsedResult, null, 2)}</pre>
            </Box>
          </Box>
        );
    }
  };

  return (
    <Container
      header={
        <Header variant="h2">
          <FormattedMessage {...messages.resultsFor} values={{ query, i: italic }} />
        </Header>
      }
    >
      <SpaceBetween size="m">
        <Alert type="info">
          <FormattedMessage
            {...messages.responseType}
            values={{ type: (parsedResult.responseType as string) || intl.formatMessage(messages.unknownTypeName), strong }}
          />
        </Alert>

        {renderResultContent()}
      </SpaceBetween>
    </Container>
  );
};

export default AgentResultDisplay;
