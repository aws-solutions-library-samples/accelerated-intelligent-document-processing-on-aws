// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// HITL › Queue Management: assign the selected items (production or Test Studio) to
// one of the Annotators listed on their queue.
import React, { useEffect, useState } from 'react';
import type { SelectProps } from '@cloudscape-design/components';
import { Box, Button, FormField, Modal, Select, SpaceBetween } from '@cloudscape-design/components';

interface AssignModalProps {
  visible: boolean;
  /** How many items are being assigned, for the prompt. */
  count: number;
  /** One item's document name, when a single item is being assigned. */
  documentLabel?: string;
  /** The current assignee, left out of the choices. */
  currentAssignee?: string | null;
  candidates: readonly string[];
  candidatesLoading: boolean;
  loading: boolean;
  onDismiss: () => void;
  onConfirm: (email: string) => void;
}

const AssignModal = ({
  visible,
  count,
  documentLabel,
  currentAssignee,
  candidates,
  candidatesLoading,
  loading,
  onDismiss,
  onConfirm,
}: AssignModalProps): React.JSX.Element => {
  const [choice, setChoice] = useState<SelectProps.Option | null>(null);
  const [submitted, setSubmitted] = useState(false);

  useEffect(() => {
    if (!visible) return;
    setChoice(null);
    setSubmitted(false);
  }, [visible]);

  const options = candidates.filter((e) => e.toLowerCase() !== currentAssignee?.toLowerCase()).map((e) => ({ label: e, value: e }));
  const what = documentLabel ?? (count === 1 ? '1 item' : `${count} items`);

  const submit = () => {
    setSubmitted(true);
    if (choice?.value) onConfirm(choice.value);
  };

  return (
    <Modal
      visible={visible}
      onDismiss={onDismiss}
      header="Assign to Annotator"
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss}>
              Cancel
            </Button>
            <Button variant="primary" loading={loading} onClick={submit}>
              Assign
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="m">
        <Box>{`Assign ${what} to an Annotator. They find it in Home › Tasks and are told in their Inbox.`}</Box>
        <FormField label="Annotator" errorText={submitted && !choice ? 'Choose an Annotator.' : undefined}>
          <Select
            selectedOption={choice}
            onChange={({ detail }) => setChoice(detail.selectedOption)}
            options={options}
            filteringType="auto"
            statusType={candidatesLoading ? 'loading' : 'finished'}
            loadingText="Loading Annotators"
            empty="No Annotators are listed on this queue. Add them under Eligible Annotators in the queue's Settings."
            placeholder="Choose an Annotator"
          />
        </FormField>
      </SpaceBetween>
    </Modal>
  );
};

export default AssignModal;
