// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Confirmation modals for the Admin actions on production review items: unassign,
// move and remove from queue. Assigning uses AssignModal.
import React, { useEffect, useState } from 'react';
import type { SelectProps } from '@cloudscape-design/components';
import { Box, Button, FormField, Modal, Select, SpaceBetween, Textarea } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, useIntl, type NoMessageValues } from 'react-intl';

import type { ReviewItem } from '../../graphql/generated/operation-types';
import { documentName } from './review-api';

type Count = { readonly count: number };
type OneDocument = { readonly document: string };

const messages = defineMessages<{
  cancel: NoMessageValues;
  unassign: NoMessageValues;
  unassignOne: { readonly document: string; readonly annotator: string; readonly queue: string };
  unassignOneNoAnnotator: { readonly document: string; readonly queue: string };
  unassignMany: Count;
  moveTitle: NoMessageValues;
  move: NoMessageValues;
  moveOne: OneDocument;
  moveMany: Count;
  targetQueue: NoMessageValues;
  queueRequired: NoMessageValues;
  noOtherQueue: NoMessageValues;
  chooseQueue: NoMessageValues;
  removeTitle: NoMessageValues;
  remove: NoMessageValues;
  removeOne: OneDocument;
  removeMany: Count;
  note: NoMessageValues;
  noteDescription: NoMessageValues;
}>({
  cancel: {
    id: 'reviewQueues.supervisorModals.cancel',
    defaultMessage: 'Cancel',
    description: 'Button of the Unassign, Move to another queue and Remove from queue dialogs that closes the dialog without acting.',
  },
  unassign: {
    id: 'reviewQueues.supervisorModals.unassign',
    defaultMessage: 'Unassign',
    description:
      'Title of the dialog that takes review items back from their Annotators, and its button that does it. The items stay in their queue, assigned to no one.',
  },
  unassignOne: {
    id: 'reviewQueues.supervisorModals.unassignOne',
    defaultMessage: 'Take {document} back from {annotator}. It stays in {queue}, unassigned; the draft is kept and the Annotator is told.',
    description:
      'Body of the Unassign dialog for one item. {document} is its file name, {annotator} the e-mail address of the Annotator it is assigned to, and {queue} the id of its review queue.',
  },
  unassignOneNoAnnotator: {
    id: 'reviewQueues.supervisorModals.unassignOneNoAnnotator',
    defaultMessage:
      'Take {document} back from its Annotator. It stays in {queue}, unassigned; the draft is kept and the Annotator is told.',
    description:
      'Body of the Unassign dialog for one item whose Annotator is not known. {document} is its file name and {queue} the id of its review queue.',
  },
  unassignMany: {
    id: 'reviewQueues.supervisorModals.unassignMany',
    defaultMessage:
      'Take {count, plural, one {# item} other {# items}} back from their Annotators. They stay in their queue, unassigned; drafts are kept and the Annotators are told.',
    description: 'Body of the Unassign dialog for several items. {count} is how many.',
  },
  moveTitle: {
    id: 'reviewQueues.supervisorModals.moveTitle',
    defaultMessage: 'Move to another queue',
    description: 'Title of the dialog that moves review items to another review queue.',
  },
  move: {
    id: 'reviewQueues.supervisorModals.move',
    defaultMessage: 'Move',
    description: 'Button of the Move to another queue dialog that moves the items to the chosen queue.',
  },
  moveOne: {
    id: 'reviewQueues.supervisorModals.moveOne',
    defaultMessage: 'Move {document} to another queue. An assignment is removed; drafts are kept.',
    description: 'Body of the Move to another queue dialog for one item. {document} is its file name.',
  },
  moveMany: {
    id: 'reviewQueues.supervisorModals.moveMany',
    defaultMessage: 'Move {count, plural, one {# item} other {# items}} to another queue. An assignment is removed; drafts are kept.',
    description: 'Body of the Move to another queue dialog for several items. {count} is how many.',
  },
  targetQueue: {
    id: 'reviewQueues.supervisorModals.targetQueue',
    defaultMessage: 'Target queue',
    description: "Label of the Move to another queue dialog's field that chooses the review queue to move the items to.",
  },
  queueRequired: {
    id: 'reviewQueues.supervisorModals.queueRequired',
    defaultMessage: 'Choose a queue.',
    description: 'Error under the Target queue field of the Move to another queue dialog when the user moves without choosing one.',
  },
  noOtherQueue: {
    id: 'reviewQueues.supervisorModals.noOtherQueue',
    defaultMessage: 'There is no other queue to move to.',
    description: 'Shown in the Target queue list of the Move to another queue dialog when there is no other review queue.',
  },
  chooseQueue: {
    id: 'reviewQueues.supervisorModals.chooseQueue',
    defaultMessage: 'Choose a queue',
    description: 'Placeholder of the Target queue field of the Move to another queue dialog.',
  },
  removeTitle: {
    id: 'reviewQueues.supervisorModals.removeTitle',
    defaultMessage: 'Remove from queue',
    description: 'Title of the dialog that takes review items out of review.',
  },
  remove: {
    id: 'reviewQueues.supervisorModals.remove',
    defaultMessage: 'Remove',
    description: 'Button of the Remove from queue dialog that takes the items out of review.',
  },
  removeOne: {
    id: 'reviewQueues.supervisorModals.removeOne',
    defaultMessage:
      'Remove {document} from review. The document completes with its machine results and leaves review. Draft corrections are not applied, and an assigned Annotator is told.',
    description: 'Body of the Remove from queue dialog for one item. {document} is its file name.',
  },
  removeMany: {
    id: 'reviewQueues.supervisorModals.removeMany',
    defaultMessage:
      'Remove {count, plural, one {# item} other {# items}} from review. The document completes with its machine results and leaves review. Draft corrections are not applied, and an assigned Annotator is told.',
    description: 'Body of the Remove from queue dialog for several items. {count} is how many.',
  },
  note: {
    id: 'reviewQueues.supervisorModals.note',
    defaultMessage: 'Note',
    description: "Label of the Remove from queue dialog's field for an optional note about why the items are removed.",
  },
  noteDescription: {
    id: 'reviewQueues.supervisorModals.noteDescription',
    defaultMessage: "Optional. Kept in the item's history.",
    description: 'Description of the Note field of the Remove from queue dialog.',
  },
});

interface BaseProps {
  /** The items the action applies to; the modal is open while non-empty. */
  items: readonly ReviewItem[];
  loading: boolean;
  onDismiss: () => void;
}

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
        <FormattedMessage {...messages.cancel} />
      </Button>
      <Button variant="primary" loading={loading} onClick={onConfirm}>
        {label}
      </Button>
    </SpaceBetween>
  </Box>
);

export const UnassignModal = ({ items, loading, onDismiss, onConfirm }: BaseProps & { onConfirm: () => void }): React.JSX.Element => {
  const intl = useIntl();
  const single = items.length === 1 ? items[0] : null;
  const unassign = intl.formatMessage(messages.unassign);
  return (
    <Modal
      visible={items.length > 0}
      onDismiss={onDismiss}
      header={unassign}
      footer={<Footer onDismiss={onDismiss} onConfirm={onConfirm} label={unassign} loading={loading} />}
    >
      <Box>
        {single
          ? single.ownerEmail != null
            ? intl.formatMessage(messages.unassignOne, {
                document: documentName(single.objectKey),
                annotator: single.ownerEmail,
                queue: single.queue,
              })
            : intl.formatMessage(messages.unassignOneNoAnnotator, { document: documentName(single.objectKey), queue: single.queue })
          : intl.formatMessage(messages.unassignMany, { count: items.length })}
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
  const intl = useIntl();
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
      header={intl.formatMessage(messages.moveTitle)}
      footer={<Footer onDismiss={onDismiss} onConfirm={submit} label={intl.formatMessage(messages.move)} loading={loading} />}
    >
      <SpaceBetween size="m">
        <Box>
          {items.length === 1
            ? intl.formatMessage(messages.moveOne, { document: documentName(items[0].objectKey) })
            : intl.formatMessage(messages.moveMany, { count: items.length })}
        </Box>
        <FormField
          label={intl.formatMessage(messages.targetQueue)}
          errorText={submitted && !choice ? intl.formatMessage(messages.queueRequired) : undefined}
        >
          <Select
            selectedOption={choice}
            onChange={({ detail }) => setChoice(detail.selectedOption)}
            options={options}
            filteringType="auto"
            empty={intl.formatMessage(messages.noOtherQueue)}
            placeholder={intl.formatMessage(messages.chooseQueue)}
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
  const intl = useIntl();
  const [note, setNote] = useState('');
  const visible = items.length > 0;
  useEffect(() => {
    if (visible) setNote('');
  }, [visible]);
  return (
    <Modal
      visible={visible}
      onDismiss={onDismiss}
      header={intl.formatMessage(messages.removeTitle)}
      footer={
        <Footer
          onDismiss={onDismiss}
          onConfirm={() => onConfirm(note.trim())}
          label={intl.formatMessage(messages.remove)}
          loading={loading}
        />
      }
    >
      <SpaceBetween size="m">
        <Box>
          {items.length === 1
            ? intl.formatMessage(messages.removeOne, { document: documentName(items[0].objectKey) })
            : intl.formatMessage(messages.removeMany, { count: items.length })}
        </Box>
        <FormField label={intl.formatMessage(messages.note)} description={intl.formatMessage(messages.noteDescription)} stretch>
          <Textarea value={note} rows={2} onChange={({ detail }) => setNote(detail.value)} />
        </FormField>
      </SpaceBetween>
    </Modal>
  );
};
