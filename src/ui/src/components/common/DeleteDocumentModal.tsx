// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React from 'react';
import { Modal, Box, SpaceBetween, Button } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, useIntl, type NoMessageValues } from 'react-intl';

const messages = defineMessages<{
  title: { readonly count: number };
  cancel: NoMessageValues;
  delete: NoMessageValues;
  body: { readonly count: number };
}>({
  title: {
    id: 'documents.deleteModal.title',
    defaultMessage: '{count, plural, =0 {Delete Document} one {Delete Document} other {Delete Documents}}',
    description: 'Title of the dialog that deletes documents. {count} is the number of documents selected.',
  },
  cancel: {
    id: 'documents.deleteModal.cancel',
    defaultMessage: 'Cancel',
    description: 'Button that closes the Delete Document dialog without deleting.',
  },
  delete: {
    id: 'documents.deleteModal.delete',
    defaultMessage: 'Delete',
    description: 'Confirm button of the Delete Document dialog.',
  },
  body: {
    id: 'documents.deleteModal.body',
    defaultMessage:
      'Are you sure you want to delete {count, plural, =0 {this document} one {this document} other {these # documents}}? This action cannot be undone.',
    description: 'Question in the Delete Document dialog. {count} is the number of documents selected.',
  },
});

interface DeleteDocumentItem {
  objectKey: string;
  name?: string;
}

interface DeleteDocumentModalProps {
  visible: boolean;
  onDismiss: () => void;
  onConfirm: () => void;
  selectedItems: readonly DeleteDocumentItem[];
  isLoading?: boolean;
}

const DeleteDocumentModal = ({
  visible,
  onDismiss,
  onConfirm,
  selectedItems,
  isLoading = false,
}: DeleteDocumentModalProps): React.JSX.Element => {
  const intl = useIntl();
  const documentCount = selectedItems.length;
  const isMultiple = documentCount > 1;

  return (
    <Modal
      visible={visible}
      onDismiss={isLoading ? undefined : onDismiss}
      header={intl.formatMessage(messages.title, { count: documentCount })}
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
        <FormattedMessage {...messages.body} values={{ count: documentCount }} />
      </p>
      {isMultiple && (
        <ul>
          {selectedItems.map((item) => (
            <li key={item.objectKey}>{item.name || item.objectKey}</li>
          ))}
        </ul>
      )}
    </Modal>
  );
};

export default DeleteDocumentModal;
