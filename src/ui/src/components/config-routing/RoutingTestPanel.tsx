// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Trying the table being edited on a processed document, before it is saved: the server
// evaluates the unsaved table against the document's results and says what it decides,
// with the reason a reviewer would read on the document.
import React, { useState } from 'react';
import {
  Alert,
  Box,
  Button,
  ExpandableSection,
  FormField,
  Input,
  KeyValuePairs,
  SpaceBetween,
  StatusIndicator,
} from '@cloudscape-design/components';
import { defineMessages, useIntl, type MessageDescriptor, type NoMessageValues } from 'react-intl';

import type { RoutingTestResult } from '../../graphql/generated/operation-types';
import { extractGraphQLErrorMessage } from '../../hooks/utils/graphql-error';
import { testInputs, testRoutingTable } from './routing-api';
import type { RoutingConfig } from './routing-types';

const messages = defineMessages<{
  header: NoMessageValues;
  intro: NoMessageValues;
  keyLabel: NoMessageValues;
  keyPlaceholder: NoMessageValues;
  test: NoMessageValues;
  errorHeader: NoMessageValues;
  decisionLabel: NoMessageValues;
  decisionHold: NoMessageValues;
  decisionContinue: NoMessageValues;
  ruleLabel: NoMessageValues;
  ruleDefault: NoMessageValues;
  queueLabel: NoMessageValues;
  queueNone: NoMessageValues;
  fallbackLabel: NoMessageValues;
  reasonLabel: NoMessageValues;
  inputsLabel: NoMessageValues;
  inputsNone: NoMessageValues;
  staleHeader: NoMessageValues;
}>({
  header: {
    id: 'routing.testPanel.header',
    defaultMessage: 'Test on a document',
    description: 'Heading of the section that tries the routing table on one document.',
  },
  intro: {
    id: 'routing.testPanel.intro',
    defaultMessage:
      'See what this table, as edited here, decides for a document that has been processed. Nothing is saved and the document is not changed.',
    description: 'Text under the heading of the routing test section.',
  },
  keyLabel: {
    id: 'routing.testPanel.keyLabel',
    defaultMessage: 'Document key',
    description: 'Label of the field that takes the object key of the document to test the routing table on.',
  },
  keyPlaceholder: {
    id: 'routing.testPanel.keyPlaceholder',
    defaultMessage: 'batch-1/invoice.pdf',
    description: 'Example object key shown in the empty document key field of the routing test.',
  },
  test: { id: 'routing.testPanel.test', defaultMessage: 'Test', description: 'Button that runs the routing table on the document.' },
  errorHeader: {
    id: 'routing.testPanel.errorHeader',
    defaultMessage: 'The table could not be tested',
    description: 'Heading of the error shown when the routing test fails.',
  },
  decisionLabel: {
    id: 'routing.testPanel.decisionLabel',
    defaultMessage: 'Decision',
    description: 'Label of the result of a routing test: whether the document would be held for review.',
  },
  decisionHold: {
    id: 'routing.testPanel.decisionHold',
    defaultMessage: 'Held for review',
    description: 'Result of a routing test: the document would wait in a review queue.',
  },
  decisionContinue: {
    id: 'routing.testPanel.decisionContinue',
    defaultMessage: 'No review: processing continues',
    description: 'Result of a routing test: the document would not be reviewed.',
  },
  ruleLabel: {
    id: 'routing.testPanel.ruleLabel',
    defaultMessage: 'Matched routing rule',
    description: 'Label of the routing rule a test matched.',
  },
  ruleDefault: {
    id: 'routing.testPanel.ruleDefault',
    defaultMessage: 'None: the default outcome applied',
    description: 'Shown as the matched routing rule of a routing test when no routing rule matched.',
  },
  queueLabel: {
    id: 'routing.testPanel.queueLabel',
    defaultMessage: 'Review queue',
    description: 'Label of the queue a routing test would place the document in.',
  },
  queueNone: {
    id: 'routing.testPanel.queueNone',
    defaultMessage: 'None',
    description: 'Shown as the queue of a routing test whose document would not be reviewed.',
  },
  fallbackLabel: {
    id: 'routing.testPanel.fallbackLabel',
    defaultMessage: 'Why the Default queue',
    description: 'Label of the reason a routing test placed the document in the Default queue instead of the queue the routing rule names.',
  },
  reasonLabel: {
    id: 'routing.testPanel.reasonLabel',
    defaultMessage: 'Reason for the reviewer',
    description:
      'Label of the reason a routing test would place the document in a review queue with: the text a reviewer reads, with the values of the tested document.',
  },
  inputsLabel: {
    id: 'routing.testPanel.inputsLabel',
    defaultMessage: 'Inputs read',
    description: 'Label of the list of values the routing test read from the document.',
  },
  inputsNone: {
    id: 'routing.testPanel.inputsNone',
    defaultMessage: 'None: no enabled routing rule reads an input.',
    description: 'Shown when a routing test read no value from the document.',
  },
  staleHeader: {
    id: 'routing.testPanel.staleHeader',
    defaultMessage: 'The table refers to things this document does not have',
    description:
      'Heading of the warning that lists what the routing table names and the tested document lacks (a class, a section result).',
  },
});

/**
 * One message per reason a document goes to the Default queue instead of the routing
 * rule's queue. The review pages show the same text (RoutedBy.tsx).
 */
const fallbackMessages = defineMessages<{
  queueMissing: NoMessageValues;
  queueDisabled: NoMessageValues;
  evaluationError: NoMessageValues;
}>({
  queueMissing: {
    id: 'routing.fallback.queueMissing',
    defaultMessage: 'The queue the routing rule names does not exist.',
    description: 'Why a document went to the Default queue: the routing rule names a queue that is not there.',
  },
  queueDisabled: {
    id: 'routing.fallback.queueDisabled',
    defaultMessage: 'The queue the routing rule names is disabled.',
    description: 'Why a document went to the Default queue: the routing rule names a queue that is switched off.',
  },
  evaluationError: {
    id: 'routing.fallback.evaluationError',
    defaultMessage: 'The table could not be evaluated.',
    description: 'Why a document went to the Default queue: the routing table failed to evaluate.',
  },
});

const messageKey = (value: string): string => value.toLowerCase().replace(/_([a-z0-9])/g, (_match, letter: string) => letter.toUpperCase());

export const fallbackMessage = (reason: string): MessageDescriptor | undefined =>
  (fallbackMessages as Record<string, MessageDescriptor>)[messageKey(reason)];

/** Where the reason of a tested document came from, by the source the server names (`admin`, `fallback`). */
const reasonSourceMessages = defineMessages<{
  admin: NoMessageValues;
  fallback: NoMessageValues;
}>({
  admin: {
    id: 'routing.testPanel.reasonSourceAdmin',
    defaultMessage: 'The reason as written, with the values of this document.',
    description:
      'Under the reason of a routing test: the text is the reason an Admin wrote for the outcome, filled in for the tested document.',
  },
  fallback: {
    id: 'routing.testPanel.reasonSourceFallback',
    defaultMessage: 'Generated: the outcome has no reason, or its reason could not be filled in for this document.',
    description: 'Under the reason of a routing test: the text was generated, because the outcome has no reason of its own or it failed.',
  },
});

/**
 * The reason a tested document would be placed with, and under it where the text came
 * from. The reason can hold values of the document: it is rendered as text. Nothing when
 * the answer holds no reason.
 */
export const TestedReason = ({ result }: { result: Pick<RoutingTestResult, 'reason' | 'reasonSource'> }): React.JSX.Element | null => {
  const intl = useIntl();
  const reason = (result.reason ?? '').trim();
  if (!reason) return null;
  const source = (reasonSourceMessages as Record<string, MessageDescriptor>)[result.reasonSource ?? ''];
  return (
    <SpaceBetween size="xxs">
      <Box>{reason}</Box>
      {source && (
        <Box variant="small" color="text-body-secondary">
          {intl.formatMessage(source)}
        </Box>
      )}
    </SpaceBetween>
  );
};

interface RoutingTestPanelProps {
  /** The table as edited, saved or not. */
  routing: RoutingConfig;
  /** The name of a queue, for display; the id when the queue is not known. */
  queueName: (queueId: string) => string;
  /** Told the answer of each test, and null when a test starts or fails. */
  onResult?: (result: RoutingTestResult | null) => void;
}

const RoutingTestPanel = ({ routing, queueName, onResult }: RoutingTestPanelProps): React.JSX.Element => {
  const intl = useIntl();
  const [objectKey, setObjectKey] = useState('');
  const [testing, setTesting] = useState(false);
  const [result, setResult] = useState<RoutingTestResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const run = async (): Promise<void> => {
    setTesting(true);
    setError(null);
    setResult(null);
    onResult?.(null);
    try {
      const answer = await testRoutingTable(objectKey.trim(), routing);
      setResult(answer);
      onResult?.(answer);
    } catch (err) {
      setError(extractGraphQLErrorMessage(err));
    } finally {
      setTesting(false);
    }
  };

  const fallbackText = (reason: string): string => {
    const message = fallbackMessage(reason);
    return message ? intl.formatMessage(message) : reason;
  };

  const inputs = result ? testInputs(result) : [];
  const queue = result?.placedQueue || result?.outcomeQueue || '';

  return (
    <ExpandableSection headerText={intl.formatMessage(messages.header)} variant="footer">
      <SpaceBetween size="s">
        <Box color="text-body-secondary">{intl.formatMessage(messages.intro)}</Box>
        <FormField label={intl.formatMessage(messages.keyLabel)}>
          <Input
            value={objectKey}
            placeholder={intl.formatMessage(messages.keyPlaceholder)}
            onChange={({ detail }) => setObjectKey(detail.value)}
          />
        </FormField>
        <Button onClick={run} loading={testing} disabled={!objectKey.trim()}>
          {intl.formatMessage(messages.test)}
        </Button>
        {error && (
          <Alert type="error" header={intl.formatMessage(messages.errorHeader)}>
            {error}
          </Alert>
        )}
        {result && (
          <SpaceBetween size="s">
            <KeyValuePairs
              columns={2}
              items={[
                {
                  label: intl.formatMessage(messages.decisionLabel),
                  value: (
                    <StatusIndicator type={result.decision === 'hold' ? 'pending' : 'success'}>
                      {intl.formatMessage(result.decision === 'hold' ? messages.decisionHold : messages.decisionContinue)}
                    </StatusIndicator>
                  ),
                },
                {
                  label: intl.formatMessage(messages.ruleLabel),
                  value: result.matchedRuleId ? result.ruleLabel || result.matchedRuleId : intl.formatMessage(messages.ruleDefault),
                },
                {
                  label: intl.formatMessage(messages.queueLabel),
                  value: result.outcomeKind === 'queue' && queue ? queueName(queue) : intl.formatMessage(messages.queueNone),
                },
                ...(result.fallbackReason
                  ? [{ label: intl.formatMessage(messages.fallbackLabel), value: fallbackText(result.fallbackReason) }]
                  : []),
              ]}
            />
            {!!(result.reason ?? '').trim() && (
              <div>
                <Box variant="awsui-key-label">{intl.formatMessage(messages.reasonLabel)}</Box>
                <TestedReason result={result} />
              </div>
            )}
            <Box variant="awsui-key-label">{intl.formatMessage(messages.inputsLabel)}</Box>
            {inputs.length === 0 ? (
              <Box color="text-body-secondary">{intl.formatMessage(messages.inputsNone)}</Box>
            ) : (
              <KeyValuePairs
                columns={3}
                items={inputs.map(([name, value]) => ({ label: name, value: typeof value === 'string' ? value : JSON.stringify(value) }))}
              />
            )}
            {(result.staleReferences ?? []).length > 0 && (
              <Alert type="warning" header={intl.formatMessage(messages.staleHeader)}>
                <ul>
                  {(result.staleReferences ?? []).map((reference) => (
                    <li key={reference}>{reference}</li>
                  ))}
                </ul>
              </Alert>
            )}
          </SpaceBetween>
        )}
      </SpaceBetween>
    </ExpandableSection>
  );
};

export default RoutingTestPanel;
