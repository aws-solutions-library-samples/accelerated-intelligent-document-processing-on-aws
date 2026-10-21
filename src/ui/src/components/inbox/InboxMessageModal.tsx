// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import React, { useEffect } from 'react';
import { Box, Button, ColumnLayout, Modal, SpaceBetween, StatusIndicator } from '@cloudscape-design/components';

import SafeMarkdown from '../common/SafeMarkdown';
import useInbox from '../../hooks/use-inbox';
import type { InboxMessage } from '../../graphql/generated/operation-types';
import { isInternalRoute } from '../../routes/constants';
import { formatDateTime } from '../../utils/date-time';
import { severityLabel, severityStatusType } from './severity';

interface InboxMessageModalProps {
  message: InboxMessage | null;
  onDismiss: () => void;
  /** Called after an unread message has been marked read by opening it. */
  onMarkedRead?: (message: InboxMessage) => void;
}

const InboxMessageModal = ({ message, onDismiss, onMarkedRead }: InboxMessageModalProps): React.JSX.Element | null => {
  const { markMessages } = useInbox();

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
      closeAriaLabel="Close message"
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss}>
              Close
            </Button>
            {isInternalRoute(link) && (
              <Button variant="primary" href={`#${link}`} onClick={onDismiss}>
                Open related page
              </Button>
            )}
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="m">
        <ColumnLayout columns={3} variant="text-grid">
          <div>
            <Box variant="awsui-key-label">Severity</Box>
            <StatusIndicator type={severityStatusType(message.severity)}>{severityLabel(message.severity)}</StatusIndicator>
          </div>
          <div>
            <Box variant="awsui-key-label">Received</Box>
            <div>{formatDateTime(message.createdAt)}</div>
          </div>
          <div>
            <Box variant="awsui-key-label">Source</Box>
            <div>{message.source}</div>
          </div>
        </ColumnLayout>
        {message.body ? <SafeMarkdown>{message.body}</SafeMarkdown> : <Box color="text-body-secondary">No details.</Box>}
      </SpaceBetween>
    </Modal>
  );
};

export default InboxMessageModal;
