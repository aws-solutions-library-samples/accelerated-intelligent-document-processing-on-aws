// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React from 'react';
import { Box, Modal, SpaceBetween, Button, Alert } from '@cloudscape-design/components';
import { ConsoleLogger } from 'aws-amplify/utils';
import { ABORTABLE_STATUSES } from './document-statuses';

const logger = new ConsoleLogger('AbortWorkflowModal');

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
  // Filter to only include items that can be aborted
  const abortableItems = selectedItems.filter((item) => item.objectStatus && ABORTABLE_STATUSES.includes(item.objectStatus));
  const nonAbortableItems = selectedItems.filter((item) => !item.objectStatus || !ABORTABLE_STATUSES.includes(item.objectStatus));

  let title = 'Abort workflow';
  let message = 'Are you sure you want to abort processing for this document?';

  if (abortableItems.length > 1) {
    title = `Abort ${abortableItems.length} workflows`;
    message = `Are you sure you want to abort processing for ${abortableItems.length} documents?`;
  } else if (abortableItems.length === 0) {
    title = 'Cannot abort workflows';
    message = 'None of the selected documents can be aborted.';
  }

  const handleConfirm = () => {
    logger.debug('Aborting workflows', abortableItems);
    onConfirm(abortableItems);
  };

  return (
    <Modal
      visible={visible}
      onDismiss={isLoading ? undefined : onDismiss}
      header={title}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss} disabled={isLoading}>
              Cancel
            </Button>
            <Button variant="primary" onClick={handleConfirm} disabled={abortableItems.length === 0} loading={isLoading}>
              Abort
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      {abortableItems.length > 0 ? (
        <>
          <p>{message}</p>
          <Alert type="warning" statusIconAriaLabel="Warning">
            This action will stop the processing workflow. Documents will be marked as &quot;ABORTED&quot; and will not complete processing.
          </Alert>
          <Box margin={{ top: 's' }}>
            <p>The following {abortableItems.length > 1 ? 'documents' : 'document'} will be aborted:</p>
            <ul>
              {abortableItems.map((item) => (
                <li key={item.objectKey}>
                  {item.objectKey} <em>({item.objectStatus})</em>
                </li>
              ))}
            </ul>
          </Box>
        </>
      ) : (
        <Alert type="info" statusIconAriaLabel="Info">
          Documents can only be aborted when they have an active workflow (status: QUEUED, RUNNING, PREPROCESSING, OCR, CLASSIFYING,
          EXTRACTING, ASSESSING, POSTPROCESSING, HITL_IN_PROGRESS, SUMMARIZING, or EVALUATING).
        </Alert>
      )}
      {nonAbortableItems.length > 0 && abortableItems.length > 0 && (
        <Box margin={{ top: 's' }}>
          <Alert type="info" statusIconAriaLabel="Info">
            {nonAbortableItems.length} {nonAbortableItems.length > 1 ? 'documents' : 'document'} cannot be aborted because{' '}
            {nonAbortableItems.length > 1 ? 'they have' : 'it has'} already completed or failed:
            <ul>
              {nonAbortableItems.map((item) => (
                <li key={item.objectKey}>
                  {item.objectKey} <em>({item.objectStatus})</em>
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
