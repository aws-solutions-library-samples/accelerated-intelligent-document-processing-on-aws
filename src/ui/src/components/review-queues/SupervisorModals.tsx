// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Confirmation modals for the Admin actions on production review items: unassign,
// move and remove from queue. Assigning uses AssignModal.
import React, { useEffect, useState } from 'react';
import type { SelectProps } from '@cloudscape-design/components';
import { Box, Button, FormField, Modal, Select, SpaceBetween, Textarea } from '@cloudscape-design/components';

import type { ReviewItem } from '../../graphql/generated/operation-types';
import { documentName } from './review-api';

interface BaseProps {
  /** The items the action applies to; the modal is open while non-empty. */
  items: readonly ReviewItem[];
  loading: boolean;
  onDismiss: () => void;
}

/** "invoice.pdf", or "3 items". */
const what = (items: readonly ReviewItem[]): string => (items.length === 1 ? documentName(items[0].objectKey) : `${items.length} items`);

const Footer = ({
  onDismiss,
  onConfirm,
  label,
  loading,
}: {
  onDismiss: () => void;
  onConfirm: () => void;
  label: string;
  loading: boolean;
}) => (
  <Box float="right">
    <SpaceBetween direction="horizontal" size="xs">
      <Button variant="link" onClick={onDismiss}>
        Cancel
      </Button>
      <Button variant="primary" loading={loading} onClick={onConfirm}>
        {label}
      </Button>
    </SpaceBetween>
  </Box>
);

export const UnassignModal = ({ items, loading, onDismiss, onConfirm }: BaseProps & { onConfirm: () => void }): React.JSX.Element => {
  const single = items.length === 1 ? items[0] : null;
  return (
    <Modal
      visible={items.length > 0}
      onDismiss={onDismiss}
      header="Unassign"
      footer={<Footer onDismiss={onDismiss} onConfirm={onConfirm} label="Unassign" loading={loading} />}
    >
      <Box>
        {single
          ? `Take ${documentName(single.objectKey)} back from ${single.ownerEmail ?? 'its Annotator'}. It stays in ${single.queue}, unassigned; the draft is kept and the Annotator is told.`
          : `Take ${what(items)} back from their Annotators. They stay in their queue, unassigned; drafts are kept and the Annotators are told.`}
      </Box>
    </Modal>
  );
};

export interface QueueOption {
  id: string;
  name: string;
}

interface MoveItemModalProps extends BaseProps {
  /** Every system-wide queue; the items' current queue is left out. */
  queues: readonly QueueOption[];
  onConfirm: (queueId: string) => void;
}

export const MoveItemModal = ({ items, loading, onDismiss, onConfirm, queues }: MoveItemModalProps): React.JSX.Element => {
  const [choice, setChoice] = useState<SelectProps.Option | null>(null);
  const [submitted, setSubmitted] = useState(false);
  const visible = items.length > 0;
  useEffect(() => {
    if (!visible) return;
    setChoice(null);
    setSubmitted(false);
  }, [visible]);
  const current = new Set(items.map((i) => i.queue));
  const options = queues
    .filter((q) => !(current.size === 1 && current.has(q.id)))
    .map((q) => ({ label: q.name, value: q.id, description: q.name === q.id ? undefined : q.id }));
  const submit = () => {
    setSubmitted(true);
    if (choice?.value) onConfirm(choice.value);
  };
  return (
    <Modal
      visible={visible}
      onDismiss={onDismiss}
      header="Move to another queue"
      footer={<Footer onDismiss={onDismiss} onConfirm={submit} label="Move" loading={loading} />}
    >
      <SpaceBetween size="m">
        <Box>{`Move ${what(items)} to another queue. An assignment is removed; drafts are kept.`}</Box>
        <FormField label="Target queue" errorText={submitted && !choice ? 'Choose a queue.' : undefined}>
          <Select
            selectedOption={choice}
            onChange={({ detail }) => setChoice(detail.selectedOption)}
            options={options}
            filteringType="auto"
            empty="No other queues. Create one in Queue Management."
            placeholder="Choose a queue"
          />
        </FormField>
      </SpaceBetween>
    </Modal>
  );
};

interface RemoveItemModalProps extends BaseProps {
  onConfirm: (note: string) => void;
}

export const RemoveItemModal = ({ items, loading, onDismiss, onConfirm }: RemoveItemModalProps): React.JSX.Element => {
  const [note, setNote] = useState('');
  const visible = items.length > 0;
  useEffect(() => {
    if (visible) setNote('');
  }, [visible]);
  return (
    <Modal
      visible={visible}
      onDismiss={onDismiss}
      header="Remove from queue"
      footer={<Footer onDismiss={onDismiss} onConfirm={() => onConfirm(note.trim())} label="Remove" loading={loading} />}
    >
      <SpaceBetween size="m">
        <Box>
          {`Remove ${what(items)} from review. The document completes with its machine results and leaves review. Draft corrections are not applied, and an assigned Annotator is told.`}
        </Box>
        <FormField label="Note" description="Optional. Kept in the item's history." stretch>
          <Textarea value={note} rows={2} onChange={({ detail }) => setNote(detail.value)} />
        </FormField>
      </SpaceBetween>
    </Modal>
  );
};
