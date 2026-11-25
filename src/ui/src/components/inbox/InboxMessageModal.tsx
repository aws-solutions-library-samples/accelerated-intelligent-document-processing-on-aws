// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import React, { useEffect } from 'react';
import { Box, Button, ColumnLayout, Modal, SpaceBetween, StatusIndicator } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, useIntl, type NoMessageValues } from 'react-intl';

import SafeMarkdown from '../common/SafeMarkdown';
import useInbox from '../../hooks/use-inbox';
import type { InboxMessage } from '../../graphql/generated/operation-types';
import { useFormatters } from '../../i18n/format';
import { isInternalRoute, withEditOrigin } from '../../routes/constants';
import { formatDateTime } from '../../utils/date-time';
import { severityLabel, severityStatusType } from './severity';

const messages = defineMessages<{
  closeAriaLabel: NoMessageValues;
  close: NoMessageValues;
  openRelated: NoMessageValues;
  severity: NoMessageValues;
  received: NoMessageValues;
  source: NoMessageValues;
  noDetails: NoMessageValues;
}>({
  closeAriaLabel: {
    id: 'inbox.messageModal.closeAriaLabel',
    defaultMessage: 'Close message',
    description: 'Screen-reader label of the close (X) button of the dialog that shows one Inbox message.',
  },
  close: {
    id: 'inbox.messageModal.close',
    defaultMessage: 'Close',
    description: 'Button in the footer of the dialog that shows one Inbox message.',
  },
  openRelated: {
    id: 'inbox.messageModal.openRelated',
    defaultMessage: 'Open related page',
    description: 'Button in the footer of an Inbox message dialog that goes to the page the message is about.',
  },
  severity: {
    id: 'inbox.messageModal.severity',
    defaultMessage: 'Severity',
    description: "Label above an Inbox message's severity (Info, Warning, ...) in its dialog.",
  },
  received: {
    id: 'inbox.messageModal.received',
    defaultMessage: 'Received',
    description: 'Label above the date and time an Inbox message arrived, in its dialog.',
  },
  source: {
    id: 'inbox.messageModal.source',
    defaultMessage: 'Source',
    description:
      'Label above the platform feature that sent an Inbox message, in its dialog. The value is shown as the platform writes it.',
  },
  noDetails: {
    id: 'inbox.messageModal.noDetails',
    defaultMessage: 'No details.',
    description: 'Shown in the dialog of an Inbox message that has a title but no body.',
  },
});

interface InboxMessageModalProps {
  message: InboxMessage | null;
  onDismiss: () => void;
  /** Called after an unread message has been marked read by opening it. */
  onMarkedRead?: (message: InboxMessage) => void;
}

const InboxMessageModal = ({ message, onDismiss, onMarkedRead }: InboxMessageModalProps): React.JSX.Element | null => {
  const intl = useIntl();
  const { markMessages } = useInbox();
  const { locale } = useFormatters();

  // Opening an unread message reads it. Silent: the user's action is opening, not
  // marking, and a failure is already reported by the provider.
  useEffect(() => {
    if (!message || message.read) return;
    markMessages([message], true, { silent: true })
      .then(() => onMarkedRead?.(message))
      .catch(() => undefined);
  }, [message?.id]);

  if (!message) return null;
  const link = message.link;

  return (
    <Modal
      visible
      onDismiss={onDismiss}
      header={message.title}
      closeAriaLabel={intl.formatMessage(messages.closeAriaLabel)}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss}>
              <FormattedMessage {...messages.close} />
            </Button>
            {isInternalRoute(link) && (
              <Button variant="primary" href={`#${withEditOrigin(link, 'inbox')}`} onClick={onDismiss}>
                <FormattedMessage {...messages.openRelated} />
              </Button>
            )}
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="m">
        <ColumnLayout columns={3} variant="text-grid">
          <div>
            <Box variant="awsui-key-label">
              <FormattedMessage {...messages.severity} />
            </Box>
            <StatusIndicator type={severityStatusType(message.severity)}>{severityLabel(message.severity, intl)}</StatusIndicator>
          </div>
          <div>
            <Box variant="awsui-key-label">
              <FormattedMessage {...messages.received} />
            </Box>
            <div>{formatDateTime(message.createdAt, locale)}</div>
          </div>
          <div>
            <Box variant="awsui-key-label">
              <FormattedMessage {...messages.source} />
            </Box>
            <div>{message.source}</div>
          </div>
        </ColumnLayout>
        {message.body ? (
          <SafeMarkdown>{message.body}</SafeMarkdown>
        ) : (
          <Box color="text-body-secondary">
            <FormattedMessage {...messages.noDetails} />
          </Box>
        )}
      </SpaceBetween>
    </Modal>
  );
};

export default InboxMessageModal;
