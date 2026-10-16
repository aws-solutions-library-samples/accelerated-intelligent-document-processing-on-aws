// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import React, { useEffect, useState } from 'react';
import type { SelectProps } from '@cloudscape-design/components';
import { Box, Button, FormField, Modal, Select, SpaceBetween, Textarea } from '@cloudscape-design/components';

export const MAX_NOTE_LENGTH = 1000;

interface RejectModalProps {
  visible: boolean;
  reasons: readonly string[];
  loading?: boolean;
  onDismiss: () => void;
  onConfirm: (reason: string, note: string | undefined) => void;
}

const RejectModal = ({ visible, reasons, loading = false, onDismiss, onConfirm }: RejectModalProps): React.JSX.Element => {
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
      header="Reject document"
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss}>
              Cancel
            </Button>
            <Button variant="primary" loading={loading} onClick={submit}>
              Reject
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="m">
        <Box>The document&apos;s review ends as rejected. Its machine output is kept and no corrections are applied.</Box>
        <FormField label="Reason" errorText={submitted && !reason ? 'Choose a reason.' : undefined}>
          <Select
            selectedOption={reason}
            onChange={({ detail }) => setReason(detail.selectedOption)}
            options={options}
            placeholder="Choose a reason"
          />
        </FormField>
        <FormField
          label="Note"
          description="Optional."
          constraintText={`${note.length}/${MAX_NOTE_LENGTH}`}
          errorText={tooLong ? `Keep the note to ${MAX_NOTE_LENGTH} characters.` : undefined}
        >
          <Textarea value={note} onChange={({ detail }) => setNote(detail.value)} rows={3} />
        </FormField>
      </SpaceBetween>
    </Modal>
  );
};

export default RejectModal;
