// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import React from 'react';
import { Box, Button, Modal, SpaceBetween } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, useIntl, type NoMessageValues } from 'react-intl';

import type { InboxMessage } from '../../graphql/generated/operation-types';

const MAX_LISTED = 10;

const messages = defineMessages<{
  header: { readonly count: number };
  body: { readonly count: number };
  more: { readonly count: number };
  cancel: NoMessageValues;
  delete: NoMessageValues;
}>({
  header: {
    id: 'inbox.deleteModal.header',
    defaultMessage: '{count, plural, one {Delete message} other {Delete messages}}',
    description: 'Title of the dialog that confirms deleting Inbox messages. {count} is the number of messages to delete.',
  },
  body: {
    id: 'inbox.deleteModal.body',
    defaultMessage:
      'Are you sure you want to delete {count, plural, one {this message} other {these # messages}}? This action cannot be undone.',
    description: 'Question of the dialog that confirms deleting Inbox messages, above their titles. {count} is the number of messages.',
  },
  more: {
    id: 'inbox.deleteModal.more',
    defaultMessage: 'and {count} more',
    description:
      'Last line of the list of message titles in the delete dialog, when it lists only the first ten. {count} is how many are not listed.',
  },
  cancel: {
    id: 'inbox.deleteModal.cancel',
    defaultMessage: 'Cancel',
    description: 'Button that closes the dialog confirming the deletion of Inbox messages without deleting them.',
  },
  delete: {
    id: 'inbox.deleteModal.delete',
    defaultMessage: 'Delete',
    description: 'Button that confirms deleting the Inbox messages the dialog lists.',
  },
});

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
  messages: targets,
  isLoading = false,
}: DeleteInboxMessagesModalProps): React.JSX.Element => {
  const intl = useIntl();
  const count = targets.length;
  const listed = targets.slice(0, MAX_LISTED);
  const more = count - listed.length;

  return (
    <Modal
      visible={visible}
      onDismiss={isLoading ? undefined : onDismiss}
      header={intl.formatMessage(messages.header, { count })}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss} disabled={isLoading}>
              <FormattedMessage {...messages.cancel} />
            </Button>
            <Button variant="primary" onClick={onConfirm} loading={isLoading}>
              <FormattedMessage {...messages.delete} />
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <p>
        <FormattedMessage {...messages.body} values={{ count }} />
      </p>
      <ul>
        {listed.map((m) => (
          <li key={m.id}>{m.title}</li>
        ))}
        {more > 0 && (
          <li>
            <FormattedMessage {...messages.more} values={{ count: more }} />
          </li>
        )}
      </ul>
    </Modal>
  );
};

export default DeleteInboxMessagesModal;
