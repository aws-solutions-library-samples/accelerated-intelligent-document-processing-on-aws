// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Ask why before a review action that is recorded in review history: a required
// reason from a fixed list and an optional note. Used by Skip on a review-queue item
// and by Skip All Reviews on a document.
import React, { useEffect, useState } from 'react';
import type { SelectProps } from '@cloudscape-design/components';
import { Box, Button, FormField, Modal, Select, SpaceBetween, Textarea } from '@cloudscape-design/components';

/** The review queues' note limit; the server refuses a longer note. */
export const MAX_NOTE_LENGTH = 1000;

interface ReasonModalProps {
  visible: boolean;
  header: string;
  /** What the action does, shown above the reason. */
  children?: React.ReactNode;
  reasons: readonly string[];
  confirmLabel: string;
  loading?: boolean;
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
  onDismiss,
  onConfirm,
}: ReasonModalProps): React.JSX.Element => {
  const [reason, setReason] = useState<SelectProps.Option | null>(null);
  const [note, setNote] = useState('');
  const [submitted, setSubmitted] = useState(false);

  useEffect(() => {
    if (visible) {
      setReason(null);
      setNote('');
      setSubmitted(false);
    }
  }, [visible]);

  const options = reasons.map((r) => ({ label: r, value: r }));
  const tooLong = note.length > MAX_NOTE_LENGTH;

  const submit = () => {
    setSubmitted(true);
    if (!reason?.value || tooLong) return;
    onConfirm(reason.value, note.trim() || undefined);
  };

  return (
    <Modal
      visible={visible}
      onDismiss={onDismiss}
      header={header}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss}>
              Cancel
            </Button>
            <Button variant="primary" loading={loading} onClick={submit}>
              {confirmLabel}
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="m">
        {children}
        <FormField label="Reason" errorText={submitted && !reason ? 'Choose a reason.' : undefined}>
          <Select
            selectedOption={reason}
            onChange={({ detail }) => setReason(detail.selectedOption)}
            options={options}
            placeholder="Choose a reason"
            ariaLabel="Reason"
          />
        </FormField>
        <FormField
          label="Note"
          description="Optional. Kept with the reason in the review history."
          constraintText={`${note.length}/${MAX_NOTE_LENGTH}`}
          errorText={tooLong ? `Keep the note to ${MAX_NOTE_LENGTH} characters.` : undefined}
        >
          <Textarea value={note} onChange={({ detail }) => setNote(detail.value)} rows={3} ariaLabel="Note" />
        </FormField>
      </SpaceBetween>
    </Modal>
  );
};

export default ReasonModal;
