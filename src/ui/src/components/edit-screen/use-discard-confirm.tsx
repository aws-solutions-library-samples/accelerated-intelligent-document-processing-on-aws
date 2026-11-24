// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The edit page's "discard your unsaved changes?" question, for an action that would
// lose them (Cancel, Refresh): Keep editing, or the action's own confirm button. What was
// already saved is kept.
import React, { useCallback, useState } from 'react';
import { Box, Button, Modal, SpaceBetween } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, useIntl, type NoMessageValues, type TypedMessageDescriptor } from 'react-intl';

const messages = defineMessages<{ body: NoMessageValues; keepEditing: NoMessageValues }>({
  body: {
    id: 'editScreen.discard.body',
    defaultMessage: 'The changes you made since the last save are lost. Anything already saved is kept.',
    description: 'Text of the dialog the edit page shows before an action, such as Cancel or Refresh, loses unsaved changes.',
  },
  keepEditing: {
    id: 'editScreen.discard.keepEditing',
    defaultMessage: 'Keep editing',
    description: 'Button of the dialog that asks before unsaved changes are lost: it closes the dialog and keeps the changes.',
  },
});

interface DiscardQuestion {
  /** The dialog's title, worded when the dialog shows. */
  header: TypedMessageDescriptor<NoMessageValues>;
  /** The confirm button's label, worded when the dialog shows. */
  confirmLabel: TypedMessageDescriptor<NoMessageValues>;
  onConfirm: () => void;
}

interface DiscardConfirm {
  /** Ask; `onConfirm` runs when the changes are to be discarded. */
  ask: (question: DiscardQuestion) => void;
  /** The confirmation dialog; render it with the screen's dialogs. */
  dialog: React.ReactNode;
}

const useDiscardConfirm = (): DiscardConfirm => {
  const intl = useIntl();
  const [question, setQuestion] = useState<DiscardQuestion | null>(null);
  const ask = useCallback((q: DiscardQuestion) => setQuestion(q), []);

  const dialog = question && (
    <Modal
      visible
      header={intl.formatMessage(question.header)}
      onDismiss={() => setQuestion(null)}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={() => setQuestion(null)}>
              <FormattedMessage {...messages.keepEditing} />
            </Button>
            <Button
              variant="primary"
              onClick={() => {
                setQuestion(null);
                question.onConfirm();
              }}
            >
              <FormattedMessage {...question.confirmLabel} />
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <FormattedMessage {...messages.body} />
    </Modal>
  );

  return { ask, dialog };
};

export default useDiscardConfirm;
