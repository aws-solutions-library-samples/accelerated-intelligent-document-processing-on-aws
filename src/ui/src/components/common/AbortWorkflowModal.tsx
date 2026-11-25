// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React from 'react';
import { Box, Modal, SpaceBetween, Button, Alert } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, useIntl, type NoMessageValues } from 'react-intl';
import { ConsoleLogger } from 'aws-amplify/utils';
import { ABORTABLE_STATUSES } from './document-statuses';
import { statusLabel } from './object-status-renderer';

const logger = new ConsoleLogger('AbortWorkflowModal');

const messages = defineMessages<{
  title: { readonly count: number };
  confirm: { readonly count: number };
  cancel: NoMessageValues;
  abort: NoMessageValues;
  warningIconAriaLabel: NoMessageValues;
  infoIconAriaLabel: NoMessageValues;
  warning: NoMessageValues;
  list: { readonly count: number };
  onlyActive: NoMessageValues;
  notAbortable: { readonly count: number };
}>({
  title: {
    id: 'documents.abortModal.title',
    defaultMessage: '{count, plural, =0 {Cannot abort workflows} one {Abort workflow} other {Abort # workflows}}',
    description: 'Title of the dialog that stops processing workflows. {count} is the number of selected documents that can be aborted.',
  },
  confirm: {
    id: 'documents.abortModal.confirm',
    defaultMessage:
      '{count, plural, =0 {None of the selected documents can be aborted.} one {Are you sure you want to abort processing for this document?} other {Are you sure you want to abort processing for # documents?}}',
    description: 'Question in the Abort dialog. {count} is the number of selected documents that can be aborted.',
  },
  cancel: {
    id: 'documents.abortModal.cancel',
    defaultMessage: 'Cancel',
    description: 'Button that closes the Abort dialog without aborting.',
  },
  abort: {
    id: 'documents.abortModal.abort',
    defaultMessage: 'Abort',
    description: 'Confirm button of the Abort dialog: stops the processing workflows.',
  },
  warningIconAriaLabel: {
    id: 'documents.abortModal.warningIconAriaLabel',
    defaultMessage: 'Warning',
    description: 'Screen-reader label of the warning icon in the Abort dialog.',
  },
  infoIconAriaLabel: {
    id: 'documents.abortModal.infoIconAriaLabel',
    defaultMessage: 'Info',
    description: 'Screen-reader label of the information icon in the Abort dialog.',
  },
  warning: {
    id: 'documents.abortModal.warning',
    defaultMessage:
      'This action will stop the processing workflow. Documents will be marked as "ABORTED" and will not complete processing.',
    description: 'Warning in the Abort dialog; "ABORTED" is the stored document status and stays as written.',
  },
  list: {
    id: 'documents.abortModal.list',
    defaultMessage: 'The following {count, plural, one {document} other {documents}} will be aborted:',
    description: 'Introduces the list of documents the Abort dialog will stop. {count} is the number of documents listed.',
  },
  onlyActive: {
    id: 'documents.abortModal.onlyActive',
    defaultMessage:
      'Documents can only be aborted when they have an active workflow (status: QUEUED, RUNNING, PREPROCESSING, OCR, CLASSIFYING, EXTRACTING, ASSESSING, POSTPROCESSING, HITL_IN_PROGRESS, SUMMARIZING, or EVALUATING).',
    description:
      'Shown in the Abort dialog when no selected document can be aborted; the status codes are stored values and stay as written.',
  },
  notAbortable: {
    id: 'documents.abortModal.notAbortable',
    defaultMessage:
      '{count, plural, one {# document cannot be aborted because it has already completed or failed:} other {# documents cannot be aborted because they have already completed or failed:}}',
    description: 'Introduces the list of selected documents the Abort dialog leaves alone. {count} is the number of documents listed.',
  },
});

interface AbortWorkflowItem {
  objectKey: string;
  objectStatus?: string;
}

interface AbortWorkflowModalProps {
  visible: boolean;
  onDismiss: () => void;
  onConfirm: (items: AbortWorkflowItem[]) => void;
  selectedItems?: readonly AbortWorkflowItem[];
  isLoading?: boolean;
}

const AbortWorkflowModal = ({
  visible,
  onDismiss,
  onConfirm,
  selectedItems = [],
  isLoading = false,
}: AbortWorkflowModalProps): React.JSX.Element => {
  const intl = useIntl();
  // Filter to only include items that can be aborted
  const abortableItems = selectedItems.filter((item) => item.objectStatus && ABORTABLE_STATUSES.includes(item.objectStatus));
  const nonAbortableItems = selectedItems.filter((item) => !item.objectStatus || !ABORTABLE_STATUSES.includes(item.objectStatus));

  const handleConfirm = () => {
    logger.debug('Aborting workflows', abortableItems);
    onConfirm(abortableItems);
  };

  // An empty status stays empty: statusLabel('') would print Unknown.
  const statusOf = (item: AbortWorkflowItem) => item.objectStatus && statusLabel(item.objectStatus, intl);

  return (
    <Modal
      visible={visible}
      onDismiss={isLoading ? undefined : onDismiss}
      header={intl.formatMessage(messages.title, { count: abortableItems.length })}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss} disabled={isLoading}>
              <FormattedMessage {...messages.cancel} />
            </Button>
            <Button variant="primary" onClick={handleConfirm} disabled={abortableItems.length === 0} loading={isLoading}>
              <FormattedMessage {...messages.abort} />
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      {abortableItems.length > 0 ? (
        <>
          <p>
            <FormattedMessage {...messages.confirm} values={{ count: abortableItems.length }} />
          </p>
          <Alert type="warning" statusIconAriaLabel={intl.formatMessage(messages.warningIconAriaLabel)}>
            <FormattedMessage {...messages.warning} />
          </Alert>
          <Box margin={{ top: 's' }}>
            <p>
              <FormattedMessage {...messages.list} values={{ count: abortableItems.length }} />
            </p>
            <ul>
              {abortableItems.map((item) => (
                <li key={item.objectKey}>
                  {item.objectKey} <em>({statusOf(item)})</em>
                </li>
              ))}
            </ul>
          </Box>
        </>
      ) : (
        <Alert type="info" statusIconAriaLabel={intl.formatMessage(messages.infoIconAriaLabel)}>
          <FormattedMessage {...messages.onlyActive} />
        </Alert>
      )}
      {nonAbortableItems.length > 0 && abortableItems.length > 0 && (
        <Box margin={{ top: 's' }}>
          <Alert type="info" statusIconAriaLabel={intl.formatMessage(messages.infoIconAriaLabel)}>
            <FormattedMessage {...messages.notAbortable} values={{ count: nonAbortableItems.length }} />
            <ul>
              {nonAbortableItems.map((item) => (
                <li key={item.objectKey}>
                  {item.objectKey} <em>({statusOf(item)})</em>
                </li>
              ))}
            </ul>
          </Alert>
        </Box>
      )}
    </Modal>
  );
};

export default AbortWorkflowModal;
