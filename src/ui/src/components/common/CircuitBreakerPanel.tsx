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
import { defineMessages, FormattedMessage, useIntl, type NoMessageValues, type TypedMessageDescriptor } from 'react-intl';

import useUserRole from '../../hooks/use-user-role';
import type { CircuitBreakerStatus } from '../../graphql/generated/operation-types';
import { formatDateTime, useFormatters } from '../../i18n/format';
import type { Locale } from '../../i18n/locales';

const logger = new ConsoleLogger('CircuitBreakerPanel');

type ActionKind = 'pause' | 'resume' | 'probe';

/** The texts of one admin action: its heading, its button, its confirm button, what it does and its success notice. */
type ActionTexts = Record<'title' | 'buttonLabel' | 'confirmLabel' | 'description' | 'submitted', NoMessageValues>;

const pauseMessages = defineMessages<ActionTexts>({
  title: {
    id: 'common.circuitBreakerPanel.pause.title',
    defaultMessage: 'Pause document processing',
    description: 'Heading of the confirmation for pausing processing.',
  },
  buttonLabel: {
    id: 'common.circuitBreakerPanel.pause.buttonLabel',
    defaultMessage: 'Pause processing',
    description: 'Admin button that opens the confirmation for pausing processing.',
  },
  confirmLabel: {
    id: 'common.circuitBreakerPanel.pause.confirmLabel',
    defaultMessage: 'Pause',
    description: 'Button that confirms pausing processing.',
  },
  description: {
    id: 'common.circuitBreakerPanel.pause.description',
    defaultMessage:
      'This will force the circuit breaker OPEN. In-flight documents continue; new documents wait in the queue until processing resumes.',
    description: 'What pausing does. OPEN is the stored state; keep it as written.',
  },
  submitted: {
    id: 'common.circuitBreakerPanel.pause.submitted',
    defaultMessage: 'Pause request submitted.',
    description: 'Notice after a pause request was sent.',
  },
});

const resumeMessages = defineMessages<ActionTexts>({
  title: {
    id: 'common.circuitBreakerPanel.resume.title',
    defaultMessage: 'Resume document processing',
    description: 'Heading of the confirmation for resuming processing.',
  },
  buttonLabel: {
    id: 'common.circuitBreakerPanel.resume.buttonLabel',
    defaultMessage: 'Resume processing',
    description: 'Admin button that opens the confirmation for resuming processing.',
  },
  confirmLabel: {
    id: 'common.circuitBreakerPanel.resume.confirmLabel',
    defaultMessage: 'Resume',
    description: 'Button that confirms resuming processing.',
  },
  description: {
    id: 'common.circuitBreakerPanel.resume.description',
    defaultMessage:
      'This will force the circuit breaker CLOSED and reset failure/recovery counters. Queued documents will resume processing.',
    description: 'What resuming does. CLOSED is the stored state; keep it as written.',
  },
  submitted: {
    id: 'common.circuitBreakerPanel.resume.submitted',
    defaultMessage: 'Resume request submitted.',
    description: 'Notice after a resume request was sent.',
  },
});

const probeMessages = defineMessages<ActionTexts>({
  title: {
    id: 'common.circuitBreakerPanel.probe.title',
    defaultMessage: 'Probe recovery',
    description: 'Heading of the confirmation for probing whether processing can recover.',
  },
  buttonLabel: {
    id: 'common.circuitBreakerPanel.probe.buttonLabel',
    defaultMessage: 'Probe recovery',
    description: 'Admin button that opens the confirmation for probing recovery.',
  },
  confirmLabel: {
    id: 'common.circuitBreakerPanel.probe.confirmLabel',
    defaultMessage: 'Probe',
    description: 'Button that confirms probing recovery.',
  },
  description: {
    id: 'common.circuitBreakerPanel.probe.description',
    defaultMessage: 'This moves the circuit breaker to HALF_OPEN to allow a small number of probe requests.',
    description: 'What probing does. HALF_OPEN is the stored state; keep it as written.',
  },
  submitted: {
    id: 'common.circuitBreakerPanel.probe.submitted',
    defaultMessage: 'Probe request submitted.',
    description: 'Notice after a probe request was sent.',
  },
});

interface ActionConfig {
  messages: Record<keyof ActionTexts, TypedMessageDescriptor<NoMessageValues>>;
  alertType: 'info' | 'warning';
}

const ACTIONS: Record<ActionKind, ActionConfig> = {
  pause: { messages: pauseMessages, alertType: 'warning' },
  resume: { messages: resumeMessages, alertType: 'info' },
  probe: { messages: probeMessages, alertType: 'info' },
};

const formatWhen = (iso: string | null | undefined, locale: Locale): string => {
  if (!iso) return '—';
  try {
    return formatDateTime(new Date(iso), locale);
  } catch {
    return iso;
  }
};

/** Each state label keeps the stored state in its text, the name the action descriptions use. */
const stateMessages = defineMessages<{ closed: NoMessageValues; halfOpen: NoMessageValues; open: NoMessageValues }>({
  closed: {
    id: 'common.circuitBreakerPanel.state.closed',
    defaultMessage: 'CLOSED (processing)',
    description: 'State of a closed circuit breaker. CLOSED is the stored state; keep it as written.',
  },
  halfOpen: {
    id: 'common.circuitBreakerPanel.state.halfOpen',
    defaultMessage: 'HALF_OPEN (recovering)',
    description: 'State of a half-open circuit breaker. HALF_OPEN is the stored state; keep it as written.',
  },
  open: {
    id: 'common.circuitBreakerPanel.state.open',
    defaultMessage: 'OPEN (paused)',
    description: 'State of an open circuit breaker. OPEN is the stored state; keep it as written.',
  },
});

const stateDisplay: Record<string, { type: StatusIndicatorProps.Type; label: TypedMessageDescriptor<NoMessageValues> }> = {
  CLOSED: { type: 'success', label: stateMessages.closed },
  HALF_OPEN: { type: 'in-progress', label: stateMessages.halfOpen },
  OPEN: { type: 'error', label: stateMessages.open },
};

const messages = defineMessages<{
  title: NoMessageValues;
  close: NoMessageValues;
  adminControls: NoMessageValues;
  requestFailed: NoMessageValues;
  reasonLabel: NoMessageValues;
  reasonDescription: NoMessageValues;
  reasonPlaceholder: NoMessageValues;
  cancel: NoMessageValues;
}>({
  title: {
    id: 'common.circuitBreakerPanel.title',
    defaultMessage: 'Circuit breaker',
    description: 'Title of the circuit breaker dialog, opened from the document list header.',
  },
  close: {
    id: 'common.circuitBreakerPanel.close',
    defaultMessage: 'Close',
    description: 'Button that closes the circuit breaker dialog.',
  },
  adminControls: {
    id: 'common.circuitBreakerPanel.adminControls',
    defaultMessage: 'Admin controls',
    description: 'Heading above the pause, resume and probe buttons of the circuit breaker dialog.',
  },
  requestFailed: {
    id: 'common.circuitBreakerPanel.requestFailed',
    defaultMessage: 'Request failed',
    description: 'Error notice of the circuit breaker dialog when an action failed without a reason.',
  },
  reasonLabel: {
    id: 'common.circuitBreakerPanel.reason.label',
    defaultMessage: 'Reason',
    description: 'Label of the field where an Admin explains a circuit breaker action.',
  },
  reasonDescription: {
    id: 'common.circuitBreakerPanel.reason.description',
    defaultMessage: 'Recorded in DynamoDB and sent to SNS subscribers.',
    description: 'Hint under the reason field: where the reason goes.',
  },
  reasonPlaceholder: {
    id: 'common.circuitBreakerPanel.reason.placeholder',
    defaultMessage: 'Explain why this action is being taken',
    description: 'Placeholder of the reason field.',
  },
  cancel: {
    id: 'common.circuitBreakerPanel.cancel',
    defaultMessage: 'Cancel',
    description: 'Button that leaves the confirmation without running the action.',
  },
});

const fieldLabels = defineMessages<{
  state: NoMessageValues;
  openedAt: NoMessageValues;
  lastChecked: NoMessageValues;
  failureCount: NoMessageValues;
  recoveryAttempts: NoMessageValues;
  lastError: NoMessageValues;
}>({
  state: {
    id: 'common.circuitBreakerPanel.field.state',
    defaultMessage: 'State',
    description: 'Label of the circuit breaker state in its dialog.',
  },
  openedAt: {
    id: 'common.circuitBreakerPanel.field.openedAt',
    defaultMessage: 'Opened at',
    description: 'Label of the time the circuit breaker opened.',
  },
  lastChecked: {
    id: 'common.circuitBreakerPanel.field.lastChecked',
    defaultMessage: 'Last checked',
    description: 'Label of the time the circuit breaker was last checked.',
  },
  failureCount: {
    id: 'common.circuitBreakerPanel.field.failureCount',
    defaultMessage: 'Failure count',
    description: 'Label of the number of failures the circuit breaker counted.',
  },
  recoveryAttempts: {
    id: 'common.circuitBreakerPanel.field.recoveryAttempts',
    defaultMessage: 'Recovery attempts',
    description: 'Label of the number of recovery attempts of the circuit breaker.',
  },
  lastError: {
    id: 'common.circuitBreakerPanel.field.lastError',
    defaultMessage: 'Last error',
    description: 'Label of the last error the circuit breaker recorded.',
  },
});

interface CircuitBreakerPanelProps {
  visible: boolean;
  status: CircuitBreakerStatus;
  onDismiss: () => void;
  onPause: (reason: string) => Promise<CircuitBreakerStatus | null>;
  onResume: (reason: string) => Promise<CircuitBreakerStatus | null>;
  onProbe: (reason: string) => Promise<CircuitBreakerStatus | null>;
}

const CircuitBreakerPanel = ({ visible, status, onDismiss, onPause, onResume, onProbe }: CircuitBreakerPanelProps): React.JSX.Element => {
  const intl = useIntl();
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
      setFeedback({ type: 'success', message: intl.formatMessage(ACTIONS[activeAction].messages.submitted) });
      setActiveAction(null);
      setReason('');
    } catch (err) {
      logger.error(`${activeAction} failed`, err);
      const gqlError = err as { errors?: { message?: string }[] };
      const detail = gqlError?.errors?.[0]?.message || (err instanceof Error ? err.message : intl.formatMessage(messages.requestFailed));
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
      header={
        <Header variant="h2">
          <FormattedMessage {...messages.title} />
        </Header>
      }
      footer={
        <Box float="right">
          <Button variant="link" onClick={onDismiss} disabled={submitting}>
            <FormattedMessage {...messages.close} />
          </Button>
        </Box>
      }
    >
      <SpaceBetween size="m">
        <KeyValuePairs
          columns={2}
          items={[
            {
              label: intl.formatMessage(fieldLabels.state),
              value: display ? (
                <StatusIndicator type={display.type}>
                  <FormattedMessage {...display.label} />
                </StatusIndicator>
              ) : (
                currentState
              ),
            },
            { label: intl.formatMessage(fieldLabels.openedAt), value: formatWhen(status.openedAt, locale) },
            { label: intl.formatMessage(fieldLabels.lastChecked), value: formatWhen(status.lastCheckedAt, locale) },
            { label: intl.formatMessage(fieldLabels.failureCount), value: String(status.failureCount ?? 0) },
            { label: intl.formatMessage(fieldLabels.recoveryAttempts), value: String(status.recoveryAttempts ?? 0) },
            { label: intl.formatMessage(fieldLabels.lastError), value: status.lastError || '—' },
          ]}
        />

        {feedback && (
          <Alert type={feedback.type} dismissible onDismiss={() => setFeedback(null)}>
            {feedback.message}
          </Alert>
        )}

        {isAdmin && !activeAction && (
          <Box>
            <Header variant="h3">
              <FormattedMessage {...messages.adminControls} />
            </Header>
            <SpaceBetween direction="horizontal" size="xs">
              <Button onClick={() => openConfirm('pause')} disabled={!canPause}>
                <FormattedMessage {...ACTIONS.pause.messages.buttonLabel} />
              </Button>
              <Button onClick={() => openConfirm('resume')} disabled={!canResume}>
                <FormattedMessage {...ACTIONS.resume.messages.buttonLabel} />
              </Button>
              <Button onClick={() => openConfirm('probe')} disabled={!canProbe}>
                <FormattedMessage {...ACTIONS.probe.messages.buttonLabel} />
              </Button>
            </SpaceBetween>
          </Box>
        )}

        {isAdmin && activeConfig && (
          <Box>
            <Header variant="h3">
              <FormattedMessage {...activeConfig.messages.title} />
            </Header>
            <SpaceBetween size="s">
              <Alert type={activeConfig.alertType}>
                <FormattedMessage {...activeConfig.messages.description} />
              </Alert>
              <FormField label={intl.formatMessage(messages.reasonLabel)} description={intl.formatMessage(messages.reasonDescription)}>
                <Textarea
                  value={reason}
                  onChange={({ detail }) => setReason(detail.value)}
                  placeholder={intl.formatMessage(messages.reasonPlaceholder)}
                  disabled={submitting}
                  rows={3}
                />
              </FormField>
              <Box float="right">
                <SpaceBetween direction="horizontal" size="xs">
                  <Button variant="link" onClick={cancelConfirm} disabled={submitting}>
                    <FormattedMessage {...messages.cancel} />
                  </Button>
                  <Button variant="primary" onClick={runAction} loading={submitting} disabled={!reason.trim()}>
                    <FormattedMessage {...activeConfig.messages.confirmLabel} />
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
