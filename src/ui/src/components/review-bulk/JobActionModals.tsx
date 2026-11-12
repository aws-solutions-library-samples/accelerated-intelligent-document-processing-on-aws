// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Home › Jobs row actions, confirmed before they run: Cancel job (a queued or running
// job stops before its next page) and Delete job (a finished job leaves the list).
import React from 'react';
import { Box, Button, Modal, SpaceBetween } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, useIntl, type MessageTag, type NoMessageValues } from 'react-intl';

export type JobAction = 'cancel' | 'delete';

interface JobActionModalProps {
  /** The action being confirmed, or null when no dialog is open. */
  action: JobAction | null;
  jobId: string;
  isLoading?: boolean;
  onDismiss: () => void;
  onConfirm: () => void;
}

const messages = defineMessages<{
  cancelHeader: NoMessageValues;
  cancelConfirm: NoMessageValues;
  cancelBody: NoMessageValues;
  deleteHeader: NoMessageValues;
  deleteConfirm: NoMessageValues;
  deleteBody: NoMessageValues;
  keepJob: NoMessageValues;
  job: { readonly jobId: string; readonly b: MessageTag };
}>({
  cancelHeader: {
    id: 'reviewBulk.jobActionModal.cancelHeader',
    defaultMessage: 'Cancel job',
    description: 'Title of the dialog confirming that a queued or running bulk Skip All Reviews job is cancelled.',
  },
  cancelConfirm: {
    id: 'reviewBulk.jobActionModal.cancelConfirm',
    defaultMessage: 'Cancel job',
    description: 'Confirm button of the Cancel job dialog.',
  },
  cancelBody: {
    id: 'reviewBulk.jobActionModal.cancelBody',
    defaultMessage:
      'The job finishes the page of up to 25 documents it is working on, then stops. Documents it has skipped stay skipped; the rest keep their pending reviews. The requester gets an Inbox message.',
    description:
      'Explanation in the Cancel job dialog of what cancelling a bulk Skip All Reviews job does. "Inbox" is the name of the message inbox.',
  },
  deleteHeader: {
    id: 'reviewBulk.jobActionModal.deleteHeader',
    defaultMessage: 'Delete job',
    description: 'Title of the dialog confirming that a finished bulk Skip All Reviews job is deleted.',
  },
  deleteConfirm: {
    id: 'reviewBulk.jobActionModal.deleteConfirm',
    defaultMessage: 'Delete job',
    description: 'Confirm button of the Delete job dialog.',
  },
  deleteBody: {
    id: 'reviewBulk.jobActionModal.deleteBody',
    defaultMessage: 'The job and its results are removed from Home › Jobs. Skipped documents stay skipped. This action cannot be undone.',
    description: 'Explanation in the Delete job dialog. "Home › Jobs" is the navigation path of the Jobs page.',
  },
  keepJob: {
    id: 'reviewBulk.jobActionModal.keepJob',
    defaultMessage: 'Keep job',
    description: 'Button that closes the Cancel job or Delete job dialog without acting.',
  },
  job: {
    id: 'reviewBulk.jobActionModal.job',
    defaultMessage: 'Job <b>{jobId}</b>',
    description: 'Names the job in the Cancel job or Delete job dialog. {jobId} is the job ID, shown in bold.',
  },
});

/** Every COPY entry is a message without arguments. */
type Copy = (typeof messages)['keepJob'];

const COPY: Record<JobAction, { header: Copy; confirm: Copy; body: Copy }> = {
  cancel: { header: messages.cancelHeader, confirm: messages.cancelConfirm, body: messages.cancelBody },
  delete: { header: messages.deleteHeader, confirm: messages.deleteConfirm, body: messages.deleteBody },
};

const JobActionModal = ({ action, jobId, isLoading = false, onDismiss, onConfirm }: JobActionModalProps): React.JSX.Element => {
  const intl = useIntl();
  const copy = COPY[action ?? 'cancel'];
  return (
    <Modal
      visible={action !== null}
      onDismiss={isLoading ? undefined : onDismiss}
      header={intl.formatMessage(copy.header)}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss} disabled={isLoading}>
              <FormattedMessage {...messages.keepJob} />
            </Button>
            <Button variant="primary" onClick={onConfirm} loading={isLoading}>
              <FormattedMessage {...copy.confirm} />
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="s">
        <Box>
          <FormattedMessage {...messages.job} values={{ jobId, b: (chunks) => <b>{chunks}</b> }} />
        </Box>
        <Box>
          <FormattedMessage {...copy.body} />
        </Box>
      </SpaceBetween>
    </Modal>
  );
};

export default JobActionModal;
