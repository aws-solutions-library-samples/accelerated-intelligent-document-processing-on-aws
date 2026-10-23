// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import React from 'react';
import { Box, Button, Modal, SpaceBetween } from '@cloudscape-design/components';

import type { InboxMessage } from '../../graphql/generated/operation-types';

const MAX_LISTED = 10;

interface DeleteInboxMessagesModalProps {
  visible: boolean;
  onDismiss: () => void;
  onConfirm: () => void;
  messages: readonly InboxMessage[];
  isLoading?: boolean;
}

const DeleteInboxMessagesModal = ({
  visible,
  onDismiss,
  onConfirm,
  messages,
  isLoading = false,
}: DeleteInboxMessagesModalProps): React.JSX.Element => {
  const count = messages.length;
  const listed = messages.slice(0, MAX_LISTED);
  const more = count - listed.length;

  return (
    <Modal
      visible={visible}
      onDismiss={isLoading ? undefined : onDismiss}
      header={count === 1 ? 'Delete message' : 'Delete messages'}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss} disabled={isLoading}>
              Cancel
            </Button>
            <Button variant="primary" onClick={onConfirm} loading={isLoading}>
              Delete
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <p>Are you sure you want to delete {count === 1 ? 'this message' : `these ${count} messages`}? This action cannot be undone.</p>
      <ul>
        {listed.map((m) => (
          <li key={m.id}>{m.title}</li>
        ))}
        {more > 0 && <li>{`and ${more} more`}</li>}
      </ul>
    </Modal>
  );
};

export default DeleteInboxMessagesModal;
