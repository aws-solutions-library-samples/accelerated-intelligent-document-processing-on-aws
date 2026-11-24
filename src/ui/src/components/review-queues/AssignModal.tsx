// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Review Studio › Review Management: assign the selected items (production or Test Studio) to
// one of the Annotators listed on their queue.
import React, { useEffect, useState } from 'react';
import type { SelectProps } from '@cloudscape-design/components';
import { Box, Button, FormField, Modal, Select, SpaceBetween } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, useIntl, type NoMessageValues } from 'react-intl';

const messages = defineMessages<{
  title: NoMessageValues;
  cancel: NoMessageValues;
  assign: NoMessageValues;
  bodyDocument: { readonly document: string };
  bodyCount: { readonly count: number };
  annotator: NoMessageValues;
  required: NoMessageValues;
  loading: NoMessageValues;
  empty: NoMessageValues;
  placeholder: NoMessageValues;
}>({
  title: {
    id: 'reviewQueues.assignModal.title',
    defaultMessage: 'Assign to Annotator',
    description: 'Title of the dialog that assigns the selected review items to an Annotator. Annotator is a user role.',
  },
  cancel: {
    id: 'reviewQueues.assignModal.cancel',
    defaultMessage: 'Cancel',
    description: 'Button of the Assign to Annotator dialog that closes it without assigning.',
  },
  assign: {
    id: 'reviewQueues.assignModal.assign',
    defaultMessage: 'Assign',
    description: 'Button of the Assign to Annotator dialog that assigns the items to the chosen Annotator.',
  },
  bodyDocument: {
    id: 'reviewQueues.assignModal.bodyDocument',
    defaultMessage: 'Assign {document} to an Annotator. They find it in Home › Tasks and are told in their Inbox.',
    description:
      'Body of the Assign to Annotator dialog for one item. {document} is its file name. "Home › Tasks" is the page path where the Annotator works the item; Inbox is the page of their messages.',
  },
  bodyCount: {
    id: 'reviewQueues.assignModal.bodyCount',
    defaultMessage:
      'Assign {count, plural, one {# item} other {# items}} to an Annotator. They find it in Home › Tasks and are told in their Inbox.',
    description:
      'Body of the Assign to Annotator dialog for several items. {count} is how many. "Home › Tasks" is the page path where the Annotator works them; Inbox is the page of their messages.',
  },
  annotator: {
    id: 'reviewQueues.assignModal.annotator',
    defaultMessage: 'Annotator',
    description: "Label of the Assign to Annotator dialog's field that chooses the Annotator, by e-mail address. Annotator is a user role.",
  },
  required: {
    id: 'reviewQueues.assignModal.required',
    defaultMessage: 'Choose an Annotator.',
    description: 'Error under the Annotator field of the Assign to Annotator dialog when the user assigns without choosing one.',
  },
  loading: {
    id: 'reviewQueues.assignModal.loading',
    defaultMessage: 'Loading Annotators',
    description: 'Shown in the Annotator list of the Assign to Annotator dialog while the Annotators are loading.',
  },
  empty: {
    id: 'reviewQueues.assignModal.empty',
    defaultMessage: "No Annotators are listed on this queue. Add them under Eligible Annotators in the queue's Settings.",
    description:
      'Shown in the Annotator list of the Assign to Annotator dialog when the queue lists no other Annotator. "Eligible Annotators" names a field of the queue\'s Settings dialog.',
  },
  placeholder: {
    id: 'reviewQueues.assignModal.placeholder',
    defaultMessage: 'Choose an Annotator',
    description: 'Placeholder of the Annotator field of the Assign to Annotator dialog.',
  },
});

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
  const intl = useIntl();
  const [choice, setChoice] = useState<SelectProps.Option | null>(null);
  const [submitted, setSubmitted] = useState(false);

  useEffect(() => {
    if (!visible) return;
    setChoice(null);
    setSubmitted(false);
  }, [visible]);

  const options = candidates.filter((e) => e.toLowerCase() !== currentAssignee?.toLowerCase()).map((e) => ({ label: e, value: e }));
  const body =
    documentLabel != null
      ? intl.formatMessage(messages.bodyDocument, { document: documentLabel })
      : intl.formatMessage(messages.bodyCount, { count });

  const submit = () => {
    setSubmitted(true);
    if (choice?.value) onConfirm(choice.value);
  };

  return (
    <Modal
      visible={visible}
      onDismiss={onDismiss}
      header={intl.formatMessage(messages.title)}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss}>
              <FormattedMessage {...messages.cancel} />
            </Button>
            <Button variant="primary" loading={loading} onClick={submit}>
              <FormattedMessage {...messages.assign} />
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="m">
        <Box>{body}</Box>
        <FormField
          label={intl.formatMessage(messages.annotator)}
          errorText={submitted && !choice ? intl.formatMessage(messages.required) : undefined}
        >
          <Select
            selectedOption={choice}
            onChange={({ detail }) => setChoice(detail.selectedOption)}
            options={options}
            filteringType="auto"
            statusType={candidatesLoading ? 'loading' : 'finished'}
            loadingText={intl.formatMessage(messages.loading)}
            empty={intl.formatMessage(messages.empty)}
            placeholder={intl.formatMessage(messages.placeholder)}
          />
        </FormField>
      </SpaceBetween>
    </Modal>
  );
};

export default AssignModal;
