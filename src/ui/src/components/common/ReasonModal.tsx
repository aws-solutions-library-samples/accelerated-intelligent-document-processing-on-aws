// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Ask why before a review action that is recorded in review history: a required
// reason from a fixed list and an optional note. Used by Skip on a review-queue item
// and by Skip All Reviews on a document.
import React, { useEffect, useState } from 'react';
import type { ModalProps, SelectProps } from '@cloudscape-design/components';
import { Box, Button, FormField, Modal, Select, SpaceBetween, Textarea } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, useIntl, type NoMessageValues } from 'react-intl';

import { fixedReasonLabel } from './review-status';

/** The review queues' note limit; the server refuses a longer note. */
export const MAX_NOTE_LENGTH = 1000;

const messages = defineMessages<{
  cancel: NoMessageValues;
  reason: NoMessageValues;
  reasonRequired: NoMessageValues;
  reasonPlaceholder: NoMessageValues;
  note: NoMessageValues;
  noteDescription: NoMessageValues;
  noteTooLong: { readonly max: number };
}>({
  cancel: {
    id: 'common.reasonModal.cancel',
    defaultMessage: 'Cancel',
    description: 'Button of the dialog that asks why before a review action (such as Skip All Reviews): closes it and does nothing.',
  },
  reason: {
    id: 'common.reasonModal.reason',
    defaultMessage: 'Reason',
    description: 'Label, and screen-reader name, of the list of reasons in the dialog that asks why before a review action.',
  },
  reasonRequired: {
    id: 'common.reasonModal.reasonRequired',
    defaultMessage: 'Choose a reason.',
    description: 'Error under the Reason list when the review action was confirmed without a reason.',
  },
  reasonPlaceholder: {
    id: 'common.reasonModal.reasonPlaceholder',
    defaultMessage: 'Choose a reason',
    description: 'Placeholder of the Reason list until a reason is chosen.',
  },
  note: {
    id: 'common.reasonModal.note',
    defaultMessage: 'Note',
    description: 'Label, and screen-reader name, of the free-text note in the dialog that asks why before a review action.',
  },
  noteDescription: {
    id: 'common.reasonModal.noteDescription',
    defaultMessage: 'Optional. Kept with the reason in the review history.',
    description: 'Help text of the Note field: a note is optional and is recorded with the reason in the review history.',
  },
  noteTooLong: {
    id: 'common.reasonModal.noteTooLong',
    defaultMessage: 'Keep the note to {max} characters.',
    description: 'Error under the Note field when the note is too long. {max} is the most characters a note may have.',
  },
});

interface ReasonModalProps {
  visible: boolean;
  header: string;
  /** What the action does, shown above the reason. */
  children?: React.ReactNode;
  reasons: readonly string[];
  confirmLabel: string;
  loading?: boolean;
  /** Confirm is disabled, showing this reason (e.g. while counting, or nothing to do). */
  confirmDisabledReason?: string | null;
  /** While true the dialog cannot be dismissed (a request is running). */
  dismissDisabled?: boolean;
  size?: ModalProps.Size;
  /** Extra controls between the summary and the reason (e.g. a checkbox). */
  extra?: React.ReactNode;
  onDismiss: () => void;
  onConfirm: (reason: string, note: string | undefined) => void;
}

const ReasonModal = ({
  visible,
  header,
  children,
  reasons,
  confirmLabel,
  loading = false,
  confirmDisabledReason = null,
  dismissDisabled = false,
  size,
  extra,
  onDismiss,
  onConfirm,
}: ReasonModalProps): React.JSX.Element => {
  const intl = useIntl();
  // The chosen reason's value; its option is built at render, so a language switch relabels it.
  const [reason, setReason] = useState<string | null>(null);
  const [note, setNote] = useState('');
  const [submitted, setSubmitted] = useState(false);

  useEffect(() => {
    if (visible) {
      setReason(null);
      setNote('');
      setSubmitted(false);
    }
  }, [visible]);

  // A fixed reason reads in the UI's language; the value sent stays the reason as given.
  const options: SelectProps.Option[] = reasons.map((r) => ({ label: fixedReasonLabel(r, intl), value: r }));
  const selectedOption = options.find((o) => o.value === reason) ?? null;
  const tooLong = note.length > MAX_NOTE_LENGTH;

  const submit = () => {
    if (confirmDisabledReason) return;
    setSubmitted(true);
    if (!reason || tooLong) return;
    onConfirm(reason, note.trim() || undefined);
  };

  return (
    <Modal
      visible={visible}
      onDismiss={dismissDisabled ? () => undefined : onDismiss}
      header={header}
      size={size}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss} disabled={dismissDisabled}>
              <FormattedMessage {...messages.cancel} />
            </Button>
            <Button
              variant="primary"
              loading={loading}
              onClick={submit}
              disabled={!!confirmDisabledReason}
              disabledReason={confirmDisabledReason ?? undefined}
            >
              {confirmLabel}
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="m">
        {children}
        {extra}
        <FormField
          label={intl.formatMessage(messages.reason)}
          errorText={submitted && !reason ? intl.formatMessage(messages.reasonRequired) : undefined}
        >
          <Select
            selectedOption={selectedOption}
            onChange={({ detail }) => setReason(detail.selectedOption.value ?? null)}
            options={options}
            placeholder={intl.formatMessage(messages.reasonPlaceholder)}
            ariaLabel={intl.formatMessage(messages.reason)}
          />
        </FormField>
        <FormField
          label={intl.formatMessage(messages.note)}
          description={intl.formatMessage(messages.noteDescription)}
          constraintText={`${note.length}/${MAX_NOTE_LENGTH}`}
          errorText={tooLong ? intl.formatMessage(messages.noteTooLong, { max: MAX_NOTE_LENGTH }) : undefined}
        >
          <Textarea value={note} onChange={({ detail }) => setNote(detail.value)} rows={3} ariaLabel={intl.formatMessage(messages.note)} />
        </FormField>
      </SpaceBetween>
    </Modal>
  );
};

export default ReasonModal;
