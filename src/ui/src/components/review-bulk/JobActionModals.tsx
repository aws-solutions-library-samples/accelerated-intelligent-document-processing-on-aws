// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Home › Jobs row actions, confirmed before they run: Cancel job (a queued or running
// job stops before its next page) and Delete job (a finished job leaves the list).
import React from 'react';
import { Box, Button, Modal, SpaceBetween } from '@cloudscape-design/components';

export type JobAction = 'cancel' | 'delete';

interface JobActionModalProps {
  /** The action being confirmed, or null when no dialog is open. */
  action: JobAction | null;
  jobId: string;
  isLoading?: boolean;
  onDismiss: () => void;
  onConfirm: () => void;
}

const COPY: Record<JobAction, { header: string; confirm: string; body: string }> = {
  cancel: {
    header: 'Cancel job',
    confirm: 'Cancel job',
    body:
      'The job finishes the page of up to 25 documents it is working on, then stops. Documents it has skipped ' +
      'stay skipped; the rest keep their pending reviews. The requester gets an Inbox message.',
  },
  delete: {
    header: 'Delete job',
    confirm: 'Delete job',
    body: 'The job and its results are removed from Home › Jobs. Skipped documents stay skipped. This action cannot be undone.',
  },
};

const JobActionModal = ({ action, jobId, isLoading = false, onDismiss, onConfirm }: JobActionModalProps): React.JSX.Element => {
  const copy = COPY[action ?? 'cancel'];
  return (
    <Modal
      visible={action !== null}
      onDismiss={isLoading ? undefined : onDismiss}
      header={copy.header}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss} disabled={isLoading}>
              Keep job
            </Button>
            <Button variant="primary" onClick={onConfirm} loading={isLoading}>
              {copy.confirm}
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="s">
        <Box>
          Job <b>{jobId}</b>
        </Box>
        <Box>{copy.body}</Box>
      </SpaceBetween>
    </Modal>
  );
};

export default JobActionModal;
