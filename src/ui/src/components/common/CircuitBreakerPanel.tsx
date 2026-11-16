// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React, { useState } from 'react';
import {
  Alert,
  Box,
  Button,
  FormField,
  Header,
  KeyValuePairs,
  Modal,
  SpaceBetween,
  StatusIndicator,
  Textarea,
} from '@cloudscape-design/components';
import type { StatusIndicatorProps } from '@cloudscape-design/components';
import { ConsoleLogger } from 'aws-amplify/utils';

import useUserRole from '../../hooks/use-user-role';
import type { CircuitBreakerStatus } from '../../graphql/generated/operation-types';
import { formatDateTime, useFormatters } from '../../i18n/format';
import type { Locale } from '../../i18n/locales';

const logger = new ConsoleLogger('CircuitBreakerPanel');

type ActionKind = 'pause' | 'resume' | 'probe';

interface ActionConfig {
  title: string;
  buttonLabel: string;
  confirmLabel: string;
  description: string;
  alertType: 'info' | 'warning';
}

const ACTIONS: Record<ActionKind, ActionConfig> = {
  pause: {
    title: 'Pause document processing',
    buttonLabel: 'Pause processing',
    confirmLabel: 'Pause',
    description:
      'This will force the circuit breaker OPEN. In-flight documents continue; new documents wait in the queue until processing resumes.',
    alertType: 'warning',
  },
  resume: {
    title: 'Resume document processing',
    buttonLabel: 'Resume processing',
    confirmLabel: 'Resume',
    description: 'This will force the circuit breaker CLOSED and reset failure/recovery counters. Queued documents will resume processing.',
    alertType: 'info',
  },
  probe: {
    title: 'Probe recovery',
    buttonLabel: 'Probe recovery',
    confirmLabel: 'Probe',
    description: 'This moves the circuit breaker to HALF_OPEN to allow a small number of probe requests.',
    alertType: 'info',
  },
};

const formatWhen = (iso: string | null | undefined, locale: Locale): string => {
  if (!iso) return '—';
  try {
    return formatDateTime(new Date(iso), locale);
  } catch {
    return iso;
  }
};

const stateDisplay: Record<string, { type: StatusIndicatorProps.Type; label: string }> = {
  CLOSED: { type: 'success', label: 'CLOSED (processing)' },
  HALF_OPEN: { type: 'in-progress', label: 'HALF_OPEN (recovering)' },
  OPEN: { type: 'error', label: 'OPEN (paused)' },
};

interface CircuitBreakerPanelProps {
  visible: boolean;
  status: CircuitBreakerStatus;
  onDismiss: () => void;
  onPause: (reason: string) => Promise<CircuitBreakerStatus | null>;
  onResume: (reason: string) => Promise<CircuitBreakerStatus | null>;
  onProbe: (reason: string) => Promise<CircuitBreakerStatus | null>;
}

const CircuitBreakerPanel = ({ visible, status, onDismiss, onPause, onResume, onProbe }: CircuitBreakerPanelProps): React.JSX.Element => {
  const { isAdmin } = useUserRole();
  const { locale } = useFormatters();
  const [activeAction, setActiveAction] = useState<ActionKind | null>(null);
  const [reason, setReason] = useState<string>('');
  const [submitting, setSubmitting] = useState<boolean>(false);
  const [feedback, setFeedback] = useState<{ type: 'success' | 'error'; message: string } | null>(null);

  const openConfirm = (action: ActionKind) => {
    setFeedback(null);
    setReason('');
    setActiveAction(action);
  };

  const cancelConfirm = () => {
    if (submitting) return;
    setActiveAction(null);
    setReason('');
  };

  const runAction = async () => {
    if (!activeAction) return;
    const trimmed = reason.trim();
    if (!trimmed) return;

    setSubmitting(true);
    try {
      if (activeAction === 'pause') await onPause(trimmed);
      else if (activeAction === 'resume') await onResume(trimmed);
      else if (activeAction === 'probe') await onProbe(trimmed);
      setFeedback({ type: 'success', message: `${ACTIONS[activeAction].confirmLabel} request submitted.` });
      setActiveAction(null);
      setReason('');
    } catch (err) {
      logger.error(`${activeAction} failed`, err);
      const gqlError = err as { errors?: { message?: string }[] };
      const detail = gqlError?.errors?.[0]?.message || (err instanceof Error ? err.message : 'Request failed');
      setFeedback({ type: 'error', message: detail });
    } finally {
      setSubmitting(false);
    }
  };

  const currentState = status.state ?? 'CLOSED';
  const display = stateDisplay[currentState];

  const canPause = currentState === 'CLOSED' || currentState === 'HALF_OPEN';
  const canResume = currentState === 'OPEN' || currentState === 'HALF_OPEN';
  const canProbe = currentState === 'OPEN';

  const activeConfig = activeAction ? ACTIONS[activeAction] : null;

  return (
    <Modal
      visible={visible}
      onDismiss={submitting ? undefined : onDismiss}
      size="medium"
      header={<Header variant="h2">Circuit breaker</Header>}
      footer={
        <Box float="right">
          <Button variant="link" onClick={onDismiss} disabled={submitting}>
            Close
          </Button>
        </Box>
      }
    >
      <SpaceBetween size="m">
        <KeyValuePairs
          columns={2}
          items={[
            {
              label: 'State',
              value: display ? <StatusIndicator type={display.type}>{display.label}</StatusIndicator> : currentState,
            },
            { label: 'Opened at', value: formatWhen(status.openedAt, locale) },
            { label: 'Last checked', value: formatWhen(status.lastCheckedAt, locale) },
            { label: 'Failure count', value: String(status.failureCount ?? 0) },
            { label: 'Recovery attempts', value: String(status.recoveryAttempts ?? 0) },
            { label: 'Last error', value: status.lastError || '—' },
          ]}
        />

        {feedback && (
          <Alert type={feedback.type} dismissible onDismiss={() => setFeedback(null)}>
            {feedback.message}
          </Alert>
        )}

        {isAdmin && !activeAction && (
          <Box>
            <Header variant="h3">Admin controls</Header>
            <SpaceBetween direction="horizontal" size="xs">
              <Button onClick={() => openConfirm('pause')} disabled={!canPause}>
                {ACTIONS.pause.buttonLabel}
              </Button>
              <Button onClick={() => openConfirm('resume')} disabled={!canResume}>
                {ACTIONS.resume.buttonLabel}
              </Button>
              <Button onClick={() => openConfirm('probe')} disabled={!canProbe}>
                {ACTIONS.probe.buttonLabel}
              </Button>
            </SpaceBetween>
          </Box>
        )}

        {isAdmin && activeConfig && (
          <Box>
            <Header variant="h3">{activeConfig.title}</Header>
            <SpaceBetween size="s">
              <Alert type={activeConfig.alertType}>{activeConfig.description}</Alert>
              <FormField label="Reason" description="Recorded in DynamoDB and sent to SNS subscribers.">
                <Textarea
                  value={reason}
                  onChange={({ detail }) => setReason(detail.value)}
                  placeholder="Explain why this action is being taken"
                  disabled={submitting}
                  rows={3}
                />
              </FormField>
              <Box float="right">
                <SpaceBetween direction="horizontal" size="xs">
                  <Button variant="link" onClick={cancelConfirm} disabled={submitting}>
                    Cancel
                  </Button>
                  <Button variant="primary" onClick={runAction} loading={submitting} disabled={!reason.trim()}>
                    {activeConfig.confirmLabel}
                  </Button>
                </SpaceBetween>
              </Box>
            </SpaceBetween>
          </Box>
        )}
      </SpaceBetween>
    </Modal>
  );
};

export default CircuitBreakerPanel;
