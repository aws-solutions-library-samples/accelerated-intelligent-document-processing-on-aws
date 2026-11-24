// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// How a review ends, asked when the reviewer chooses Process Changes on a Production
// item: complete it (the corrections are kept and processing continues), or send the
// document back to be processed again (the corrections and any page regrouping are
// discarded, and the dialog says so). Sending back takes an optional note, which goes
// into the review's history.
//
// The reviewer chooses the ending and nothing else: where the run starts is the
// server's decision. For the ending chosen, the dialog shows what the server answers
// (getReviewCompletionPlan, read once for both endings while the dialog is open): the
// step the run starts at, each step with whether it runs, and the sections extracted
// again. When that cannot be read the dialog says so and offers Retry, and the review
// can still be finished.
import React, { useCallback, useEffect, useState } from 'react';
import { Alert, Box, Button, FormField, Modal, RadioGroup, SpaceBetween, Textarea } from '@cloudscape-design/components';
import { defineMessages, useIntl, type NoMessageValues } from 'react-intl';

import type { ReviewCompletionPlan } from '../../graphql/generated/operation-types';
import { MAX_NOTE_LENGTH } from '../common/ReasonModal';
import PlannedSteps from '../run-plan/PlannedSteps';
import { getReviewCompletionPlan } from '../run-plan/run-plan-api';
import usePlannedRun from '../run-plan/use-planned-run';

export type ReviewOutcome = 'complete' | 'send-back';

const messages = defineMessages<{
  header: NoMessageValues;
  outcomeLabel: NoMessageValues;
  complete: NoMessageValues;
  completeDescription: NoMessageValues;
  sendBack: NoMessageValues;
  sendBackDescription: NoMessageValues;
  discardWarning: NoMessageValues;
  noteLabel: NoMessageValues;
  noteDescription: NoMessageValues;
  noteTooLong: { readonly max: number };
  errorHeader: NoMessageValues;
  cancel: NoMessageValues;
  confirmComplete: NoMessageValues;
  confirmSendBack: NoMessageValues;
}>({
  header: {
    id: 'reviewQueues.completionModal.header',
    defaultMessage: 'Finish the review',
    description: 'Header of the dialog that asks how a review ends, opened by Process Changes on a review item.',
  },
  outcomeLabel: {
    id: 'reviewQueues.completionModal.outcomeLabel',
    defaultMessage: 'How does this review end?',
    description: 'Label of the choice between completing a review and sending the document back.',
  },
  complete: {
    id: 'reviewQueues.completionModal.complete',
    defaultMessage: 'Complete the review',
    description: 'Review outcome option: the review is finished with the corrections made.',
  },
  completeDescription: {
    id: 'reviewQueues.completionModal.completeDescription',
    defaultMessage: 'Your corrections are kept and processing continues with them.',
    description: 'Help under the Complete the review option.',
  },
  sendBack: {
    id: 'reviewQueues.completionModal.sendBack',
    defaultMessage: 'Send back for reprocessing',
    description: 'Review outcome option: the document is processed again, without this review. The system decides where the run starts.',
  },
  sendBackDescription: {
    id: 'reviewQueues.completionModal.sendBackDescription',
    defaultMessage: 'The document is processed again without this review. It can return to a review queue afterwards.',
    description: 'Help under the Send back for reprocessing option.',
  },
  discardWarning: {
    id: 'reviewQueues.completionModal.discardWarning',
    defaultMessage:
      'The corrections and any page regrouping are discarded, and the document is processed again from classification (from the beginning in BDA mode).',
    description:
      'Warning shown when Send back for reprocessing is chosen. Classification is a processing step; BDA mode is the processing mode that uses Bedrock Data Automation.',
  },
  noteLabel: {
    id: 'reviewQueues.completionModal.noteLabel',
    defaultMessage: 'Note (optional)',
    description: 'Label of the free-text note of a send-back.',
  },
  noteDescription: {
    id: 'reviewQueues.completionModal.noteDescription',
    defaultMessage: 'Why the document is sent back. Kept in the review history.',
    description: 'Help under the label of the note of a send-back.',
  },
  noteTooLong: {
    id: 'reviewQueues.completionModal.noteTooLong',
    defaultMessage: 'Keep the note to {max, number} characters.',
    description: 'Error under the note of a send-back that is too long. {max} is the largest number of characters.',
  },
  errorHeader: {
    id: 'reviewQueues.completionModal.errorHeader',
    defaultMessage: 'The document was not sent back',
    description: 'Heading of the error in the review completion dialog when a send-back failed or was refused.',
  },
  cancel: {
    id: 'reviewQueues.completionModal.cancel',
    defaultMessage: 'Cancel',
    description: 'Button that closes the review completion dialog.',
  },
  confirmComplete: {
    id: 'reviewQueues.completionModal.confirmComplete',
    defaultMessage: 'Complete review',
    description: 'Primary button of the review completion dialog when Complete the review is chosen.',
  },
  confirmSendBack: {
    id: 'reviewQueues.completionModal.confirmSendBack',
    defaultMessage: 'Send back',
    description: 'Primary button of the review completion dialog when Send back for reprocessing is chosen.',
  },
});

interface ReviewCompletionModalProps {
  visible: boolean;
  /** The review item the dialog finishes: its document, and the review cycle it is in. */
  objectKey: string;
  cycleId: string;
  /** Offer Send back for reprocessing. Without it the dialog confirms completion only. */
  canSendBack: boolean;
  /** The outcome being carried out; the dialog cannot be changed or dismissed meanwhile. */
  busy: ReviewOutcome | null;
  /** Why the last send-back did not go through, in words. */
  error?: string | null;
  onComplete: () => void;
  onSendBack: (note: string | undefined) => void;
  onCancel: () => void;
}

const ReviewCompletionModal = ({
  visible,
  objectKey,
  cycleId,
  canSendBack,
  busy,
  error = null,
  onComplete,
  onSendBack,
  onCancel,
}: ReviewCompletionModalProps): React.JSX.Element => {
  const intl = useIntl();
  const [outcome, setOutcome] = useState<ReviewOutcome>('complete');
  const [note, setNote] = useState('');
  // What each ending runs: one read for both, made while the dialog is open.
  const loadPlan = useCallback(() => getReviewCompletionPlan(objectKey, cycleId), [objectKey, cycleId]);
  const planned = usePlannedRun<ReviewCompletionPlan>(loadPlan, visible);

  // Each opening starts from the same choice: a dialog reopened after Cancel must not
  // come back set to send the document back.
  useEffect(() => {
    if (!visible) return;
    setOutcome('complete');
    setNote('');
  }, [visible]);

  const working = busy !== null;
  const sendingBack = outcome === 'send-back';
  const noteTooLong = note.length > MAX_NOTE_LENGTH;
  const plan = planned.plan && (sendingBack ? planned.plan.sendBack : planned.plan.complete);

  // The button is disabled while the note is too long, so a send-back always has a note that fits.
  const confirm = (): void => {
    if (!sendingBack) onComplete();
    else onSendBack(note.trim() || undefined);
  };

  return (
    <Modal
      visible={visible}
      onDismiss={working ? () => undefined : onCancel}
      header={intl.formatMessage(messages.header)}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" disabled={working} onClick={onCancel}>
              {intl.formatMessage(messages.cancel)}
            </Button>
            <Button variant="primary" loading={working} disabled={sendingBack && noteTooLong} onClick={confirm}>
              {intl.formatMessage(sendingBack ? messages.confirmSendBack : messages.confirmComplete)}
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="m">
        {error && (
          <Alert type="error" header={intl.formatMessage(messages.errorHeader)}>
            {error}
          </Alert>
        )}
        <FormField label={intl.formatMessage(messages.outcomeLabel)}>
          <RadioGroup
            value={outcome}
            onChange={({ detail }) => setOutcome(detail.value as ReviewOutcome)}
            items={[
              {
                value: 'complete',
                label: intl.formatMessage(messages.complete),
                description: intl.formatMessage(messages.completeDescription),
                disabled: working,
              },
              ...(canSendBack
                ? [
                    {
                      value: 'send-back',
                      label: intl.formatMessage(messages.sendBack),
                      description: intl.formatMessage(messages.sendBackDescription),
                      disabled: working,
                    },
                  ]
                : []),
            ]}
          />
        </FormField>
        {sendingBack && <Alert type="warning">{intl.formatMessage(messages.discardWarning)}</Alert>}
        {/* Completing does not wait for this: a failed read shows as a warning with Retry. */}
        <PlannedSteps plan={plan} loading={planned.loading} error={planned.error} onRetry={planned.retry} />
        {sendingBack && (
          <FormField
            label={intl.formatMessage(messages.noteLabel)}
            description={intl.formatMessage(messages.noteDescription)}
            errorText={noteTooLong ? intl.formatMessage(messages.noteTooLong, { max: MAX_NOTE_LENGTH }) : undefined}
            stretch
          >
            <Textarea value={note} rows={3} disabled={working} onChange={({ detail }) => setNote(detail.value)} />
          </FormField>
        )}
      </SpaceBetween>
    </Modal>
  );
};

export default ReviewCompletionModal;
